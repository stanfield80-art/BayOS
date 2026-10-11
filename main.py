"""
BayOS Gateway - Core Task Ingestion & Agent Routing Engine
Version: 2.4.0
Status: Production Hardened
"""

import asyncio
import hmac
import logging
import os
import re
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security, status
from fastapi.security import APIKeyHeader
from google import genai
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# --- Logging & Initialization ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bayos.kernel")


# 1. Fail-Closed Authentication Configuration
BAYOS_AUTH_KEY = os.environ.get("BAYOS_AUTH_KEY")
if not BAYOS_AUTH_KEY:
    raise RuntimeError("BAYOS_AUTH_KEY is not set. Set it as an environment variable before starting the gateway.")

API_KEY_NAME = "X-BayOS-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

# Local smoke tests set BAYOS_INFERENCE_MODE=stub so ingest can be exercised
# without calling Gemini. Cloud Run must leave this unset (default: gemini).
_startup_inference_mode = os.environ.get("BAYOS_INFERENCE_MODE", "gemini").strip().lower() or "gemini"
if _startup_inference_mode == "stub":
    logger.warning("BAYOS_INFERENCE_MODE=stub: Gemini is not called. Do not set this on Cloud Run.")


async def verify_auth_key(api_key: Optional[str] = Security(api_key_header)) -> str:
    """Enforce fail-closed API key verification."""
    if not BAYOS_AUTH_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication not configured on server (BAYOS_AUTH_KEY missing).",
        )
    if not api_key or not hmac.compare_digest(api_key, BAYOS_AUTH_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )
    return api_key


# 2. Request Body Size Limit Middleware
MAX_CONTENT_LENGTH = 1_048_576  # 1 MB


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_CONTENT_LENGTH:
                    return JSONResponse(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        content={"detail": f"Payload exceeds limit of {MAX_CONTENT_LENGTH} bytes."},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header."},
                )
        return await call_next(request)


# 3. Sliding-Window In-Memory Rate Limiter
class SlidingWindowRateLimiter:
    def __init__(self, requests_per_minute: int = 60):
        self.rpm = requests_per_minute
        self.window = 60.0
        self.hits: Dict[str, deque] = defaultdict(deque)

    def is_allowed(self, client_id: str) -> bool:
        now = time.time()
        timestamps = self.hits[client_id]
        while timestamps and timestamps[0] <= now - self.window:
            timestamps.popleft()
        if len(timestamps) >= self.rpm:
            return False
        timestamps.append(now)
        return True


rate_limiter = SlidingWindowRateLimiter(requests_per_minute=60)


async def check_rate_limit(request: Request, api_key: str = Depends(verify_auth_key)) -> None:
    client_id = api_key or request.client.host
    if not rate_limiter.is_allowed(client_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded (60 requests per minute).",
        )


# 4. In-memory inbox.
# Cloud Run with min-instances 0 drops this deque when the instance shuts down.
# A cheap durable replacement is Cloud Firestore (Native mode), collection "inbox":
# write one document per ingest and list the newest documents from GET /api/v1/inbox.
# That store is not wired up here.
INBOX_CAPACITY = 100
MAX_STORED_TASK_CHARS = 20_000
MAX_STORED_OUTPUT_CHARS = 50_000
INBOX_NOTICE = (
    "In-memory only. This list is wiped when the Cloud Run instance scales to zero, "
    "and separate instances do not share it. A cheap durable replacement is Cloud Firestore "
    "(Native mode), collection inbox."
)

_inbox: Deque["InboxItem"] = deque(maxlen=INBOX_CAPACITY)
_gemini_client: Optional[genai.Client] = None


def clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n[truncated]"
    return text[: max(0, limit - len(marker))] + marker


def inference_mode() -> str:
    return os.environ.get("BAYOS_INFERENCE_MODE", "gemini").strip().lower() or "gemini"


def normalize_gemini_api_key(raw: Optional[str]) -> str:
    """Return the Gemini key with paste artifacts removed.

    AI Studio auth keys start with AQ. Older standard keys start with AIza.
    Both are API keys. A copied BOM, wrapping quotes, or a line break makes
    Google reject the value as an unsupported access token.
    """
    if not raw:
        return ""
    key = raw.replace("\ufeff", "").strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1].strip()
    return "".join(key.split())


def gemini_api_key() -> str:
    return normalize_gemini_api_key(os.environ.get("GEMINI_API_KEY"))


# Current generateContent samples use gemini-3.8-flash on v1beta.
# gemini-2.5-flash still exists, but Google limits it to projects that have
# already used the 2.5 models. New projects are told to use 3.8 Flash or
# 3.5 Flash-Lite. GEMINI_MODEL overrides the default.
_GEMINI_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


def gemini_model() -> str:
    raw = (os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip()
    if _GEMINI_MODEL_RE.match(raw):
        return raw
    return DEFAULT_GEMINI_MODEL


def _force_api_key_header(client: genai.Client, api_key: str) -> None:
    """Put the key in x-goog-api-key and drop any Authorization header.

    The Generative Language API expects that header for both AQ. and AIza keys.
    Sending either value as Authorization: Bearer returns 401
    ACCESS_TOKEN_TYPE_UNSUPPORTED on generateContent.
    """

    def wrap(send):
        def wrapped(request, *args, **kwargs):
            request.headers["x-goog-api-key"] = api_key
            if "authorization" in request.headers:
                del request.headers["authorization"]
            return send(request, *args, **kwargs)

        return wrapped

    api_client = client._api_client
    api_client._httpx_client.send = wrap(api_client._httpx_client.send)
    async_client = getattr(api_client, "_async_httpx_client", None)
    if async_client is not None:
        async_client.send = wrap(async_client.send)


_gemini_client_key: Optional[str] = None


def get_gemini_client() -> genai.Client:
    """Create a Gemini Developer API client for the configured key.

    vertexai stays off so Cloud Run's service account is not used as an OAuth
    bearer token. The key is passed explicitly so GOOGLE_API_KEY cannot replace
    GEMINI_API_KEY.
    """
    global _gemini_client, _gemini_client_key
    key = gemini_api_key()
    if _gemini_client is None or _gemini_client_key != key:
        client = genai.Client(
            api_key=key,
            vertexai=False,
            http_options={"headers": {"x-goog-api-key": key}},
        )
        _force_api_key_header(client, key)
        _gemini_client = client
        _gemini_client_key = key
    return _gemini_client


def _inference_failure_detail(exc: Exception) -> str:
    text = str(exc)
    detail = f"Inference failure: {text}"
    if "ACCESS_TOKEN_TYPE_UNSUPPORTED" in text or "UNAUTHENTICATED" in text:
        key = gemini_api_key()
        if key.startswith("AQ."):
            kind = "AQ. auth key"
        elif key.startswith("AIza"):
            kind = "AIza standard key"
        else:
            kind = "key"
        detail += (
            f" The gateway sent the {kind} (length {len(key)}) in the x-goog-api-key header "
            "to generativelanguage.googleapis.com/v1beta. Google checks that key before it looks at "
            f"the model ({gemini_model()}). An AQ. auth key is bound to a service account in the key's "
            "own project. The Cloud Run project does not have to be the same project, and this gateway "
            "does not send an OAuth bearer token. Google returns this 401 when it cannot validate the "
            "AQ. string, including when the copied value is incomplete. Re-copy the full key from AI Studio, "
            "or paste an AIza key from Cloud Console Credentials that is restricted to the Generative Language API. "
            "That API must be enabled in the key's project."
        )
    return detail


# --- Application Setup ---
app = FastAPI(
    title="BayOS Gateway",
    version="2.4.0",
    docs_url="/docs",
    redoc_url=None,
)
app.add_middleware(BodySizeLimitMiddleware)

# 5. Full 28-Agent System Registry
AGENT_PROMPTS: Dict[str, str] = {
    # Foundry & Executive Command
    "prof-x-001": "You are POLE: Master intake orchestrator, task classifier, and Action Digest synthesizer.",
    "council-001": "You are Council: Constitutional auditor; evaluates actions against the 8 Core Laws.",
    "forge-001": "You are Forge: System architect; designs modular workflows, agent blueprints, and schemas.",
    "reverse-001": "You are Reverse: Inversion engineer; backward-chains outcomes to prerequisites using 9-Point Blueprints.",
    "smith-001": "You are Smith: Schema gatekeeper; validates Markdown formatting, YAML frontmatter, and JSON contracts.",
    "darwin-001": "You are Darwin: System tuner; identifies workflow friction, prunes prompt bloat, and optimizes agent prompts.",
    "vanguard-001": "You are Vanguard: Security auditor; enforces zero-trust boundaries and screens external API integrations.",
    "wolverine-001": "You are Wolverine: Fault isolation engine; diagnoses errors, maps blast radius, and prepares rollback steps.",
    # Critical Thinking & Wisdom
    "socrates-001": "You are Socrates: Premise stress-tester; uncovers hidden assumptions via first-principles inquiry.",
    "sherlock-001": "You are Sherlock: Forensic investigator; analyzes evidence, resolves contradictions, and reconstructs timelines.",
    "prism-001": "You are Prism: Perspective generator; models complex decisions across multiple strategic lenses.",
    "yoda-001": "You are Yoda: Synthesizer; compresses complex inputs into single-sentence baseline principles.",
    "mirror-001": "You are Mirror: Cognitive sounding board; clarifies ambiguous intent and reframes mental blocks.",
    "logic-001": "You are Logic: Reasoning auditor; evaluates arguments for cognitive biases and logical fallacies.",
    # Financial Strategy & Capital
    "dave-001": "You are Dave: Debt eliminator; enforces zero-based cash flow and the Debt Snowball method.",
    "envelope-001": "You are Envelope: Allocation tracker; manages category caps and the Four Walls framework.",
    "tally-001": "You are Tally: Arithmetic reconciler; parses receipts and verifies numerical balance sheets.",
    "ledger-001": "You are Ledger: Tax modeling assistant; categorizes deductible expenses and audit trails.",
    "scout-001": "You are Scout: Market analyst; scans listings, checks baseline values, and calculates resale margins.",
    "harvest-001": "You are Harvest: Expense auditor; tracks recurring subscriptions, fee spikes, and unused services.",
    "negotiator-001": "You are Negotiator: Script generator; drafts evidence-backed counteroffers for bills and contracts.",
    # Operations & Logistics
    "phoenix-001": "You are Phoenix: Resilience specialist; generates credit repair plans and dispute worksheets.",
    "chronos-001": "You are Chronos: Time architect; resolves schedule collisions, sets time blocks, and tracks milestones.",
    "pony-001": "You are Pony Express: Correspondence manager; triages incoming messages and drafts clean outbound replies.",
    "beacon-001": "You are Beacon: Research extractor; filters web noise into sourced, high-signal briefings.",
    "murdock-001": "You are Murdock: Legal & tactical analyst; translates complex contract clauses into actionable options.",
    # Learning & Health
    "baymax-001": "You are Clinical Baymax: Health & study guide; structures clinical reasoning frameworks and wellness routines.",
    "dojo-001": "You are Dojo: Drillmaster; creates active-recall quizzes, simulation cases, and deliberate practice drills.",
}


# --- Schemas ---
class TaskIngestRequest(BaseModel):
    task: str = Field(..., min_length=1, description="Task prompt or command.")
    agent: str = Field(default="prof-x-001", description="Target agent identifier.")
    context: Optional[Dict[str, Any]] = Field(default=None, description="Optional runtime metadata.")


class TaskIngestResponse(BaseModel):
    agent: str
    status: str
    output: str
    version: str = "2.4.0"


class InboxItem(BaseModel):
    id: str
    received_at: str
    agent: str
    task: str
    status: str
    output: str
    version: str = "2.4.0"


class InboxListResponse(BaseModel):
    storage: str
    durable: bool
    cleared_on_scale_to_zero: bool
    notice: str
    total: int
    count: int
    items: List[InboxItem]


async def run_inference(agent_id: str, system_instruction: str, task: str) -> str:
    """Call Gemini, or return a local acknowledgement when stub mode is on."""
    mode = inference_mode()
    if mode == "stub":
        return f"[stub] accepted by {agent_id}"
    if mode != "gemini":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Unknown BAYOS_INFERENCE_MODE '{mode}'. "
                "Use gemini, or stub for a local smoke test only."
            ),
        )
    if not gemini_api_key():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GEMINI_API_KEY is not configured.",
        )

    client = get_gemini_client()
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=gemini_model(),
            contents=task,
            config={"system_instruction": system_instruction},
        )
        return response.text or ""
    except Exception as exc:
        logger.error("Inference failure for agent %s: %s", agent_id, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_inference_failure_detail(exc),
        ) from exc


# --- Routes ---
@app.get("/health", tags=["Telemetry"])
async def health_check():
    """6. Version drift eliminated: returns synchronized 2.4.0 runtime state."""
    return {
        "status": "healthy",
        "service": "BayOS Gateway",
        "version": "2.4.0",
        "registered_agents": len(AGENT_PROMPTS),
        "auth_configured": bool(BAYOS_AUTH_KEY),
        "gemini_configured": bool(gemini_api_key()),
        "gemini_model": gemini_model(),
        "inference_mode": inference_mode(),
        "inbox_storage": "memory",
    }


@app.get(
    "/api/v1/inbox",
    response_model=InboxListResponse,
    dependencies=[Depends(verify_auth_key), Depends(check_rate_limit)],
    tags=["Inbox"],
)
async def list_inbox(limit: int = Query(default=20, ge=1, le=100)):
    """List recently ingested tasks, newest first."""
    ordered = list(_inbox)
    ordered.reverse()
    selected = ordered[:limit]
    return InboxListResponse(
        storage="memory",
        durable=False,
        cleared_on_scale_to_zero=True,
        notice=INBOX_NOTICE,
        total=len(_inbox),
        count=len(selected),
        items=selected,
    )


@app.post(
    "/api/v1/task/ingest",
    response_model=TaskIngestResponse,
    dependencies=[Depends(verify_auth_key), Depends(check_rate_limit)],
    tags=["Ingestion"],
)
async def ingest_task(payload: TaskIngestRequest):
    """
    Ingests, validates, and routes tasks to target BayOS agents.
    Records the task in the in-memory inbox so it can be read back.
    4. Offloads blocking Gemini API calls to an async threadpool.
    """
    agent_id = payload.agent.lower()
    system_instruction = AGENT_PROMPTS.get(agent_id)

    if not system_instruction:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{payload.agent}' not recognized. Registered count: {len(AGENT_PROMPTS)}.",
        )

    record = InboxItem(
        id=uuid.uuid4().hex,
        received_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        agent=agent_id,
        task=clip_text(payload.task, MAX_STORED_TASK_CHARS),
        status="pending",
        output="",
        version="2.4.0",
    )
    _inbox.append(record)

    try:
        output_text = await run_inference(agent_id, system_instruction, payload.task)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        record.status = "failed"
        record.output = clip_text(detail, MAX_STORED_OUTPUT_CHARS)
        logger.info("Ingest id=%s agent=%s status=failed", record.id, agent_id)
        raise

    record.status = "completed"
    record.output = clip_text(output_text, MAX_STORED_OUTPUT_CHARS)
    logger.info("Ingest id=%s agent=%s status=completed", record.id, agent_id)
    return TaskIngestResponse(
        agent=agent_id,
        status="completed",
        output=output_text,
        version="2.4.0",
    )
