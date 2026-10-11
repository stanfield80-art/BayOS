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
import threading
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


# 4. Inbox. Firestore Native collection "inbox" is the durable store.
# The in-memory deque is the fallback when that database cannot be reached.
INBOX_CAPACITY = 100
INBOX_SCAN_LIMIT = 1000
INBOX_COLLECTION = "inbox"
MAX_STORED_TASK_CHARS = 20_000
MAX_STORED_OUTPUT_CHARS = 50_000
_INBOX_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_PRIORITIES = {"low", "normal", "high"}

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


_LOCATION_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")


def gemini_project() -> str:
    return os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()


def gemini_location() -> str:
    """Vertex location. gemini-3.8-flash is on global, us, and eu, not us-central1."""
    raw = (os.environ.get("GOOGLE_CLOUD_LOCATION") or "global").strip()
    if _LOCATION_RE.match(raw):
        return raw
    return "global"


def gemini_backend() -> str:
    """Vertex ADC is the default. The API key is used only when asked for, or when no project is set and a key is present.

    An explicit BAYOS_GEMINI_BACKEND=vertex never falls through to the API key.
    A full AQ. key in x-goog-api-key still returns 401 ACCESS_TOKEN_TYPE_UNSUPPORTED
    on generativelanguage.googleapis.com, including as ?key= and through this SDK.
    """
    raw = os.environ.get("BAYOS_GEMINI_BACKEND", "").strip().lower()
    if raw in {"apikey", "api-key", "api_key"}:
        return "apikey"
    if raw in {"vertex", "adc"}:
        return "vertex"
    if gemini_project():
        return "vertex"
    if gemini_api_key():
        return "apikey"
    return "vertex"


def gemini_ready() -> bool:
    if gemini_backend() == "vertex":
        return bool(gemini_project())
    return bool(gemini_api_key())


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


def _client_cache_key() -> str:
    if gemini_backend() == "vertex":
        return f"vertex:{gemini_project()}:{gemini_location()}:{gemini_model()}"
    return f"apikey:{gemini_api_key()}"


def get_gemini_client() -> genai.Client:
    """Create the Gemini client for the selected backend.

    Vertex uses Application Default Credentials on Cloud Run. The runtime
    service account's OAuth token is sent to aiplatform.googleapis.com.
    The API-key backend keeps AQ. and AIza values in x-goog-api-key only.
    """
    global _gemini_client, _gemini_client_key
    cache_key = _client_cache_key()
    if _gemini_client is not None and _gemini_client_key == cache_key:
        return _gemini_client
    if gemini_backend() == "vertex":
        client = genai.Client(
            vertexai=True,
            project=gemini_project(),
            location=gemini_location(),
            http_options={"api_version": "v1"},
        )
    else:
        key = gemini_api_key()
        client = genai.Client(
            api_key=key,
            vertexai=False,
            http_options={"headers": {"x-goog-api-key": key}},
        )
        _force_api_key_header(client, key)
    _gemini_client = client
    _gemini_client_key = cache_key
    return _gemini_client


def _inference_failure_detail(exc: Exception) -> str:
    text = str(exc)
    detail = f"Inference failure: {text}"
    if gemini_backend() == "vertex":
        detail += (
            f" Vertex AI model {gemini_model()} at location {gemini_location()} "
            f"in project {gemini_project() or '(unset)'}. "
            "Enable aiplatform.googleapis.com and grant the Cloud Run service account roles/aiplatform.user. "
            "gemini-3.8-flash is available on the global, us, and eu endpoints."
        )
        return detail
    if "ACCESS_TOKEN_TYPE_UNSUPPORTED" in text or "UNAUTHENTICATED" in text:
        detail += (
            " The API-key path sent the key in x-goog-api-key to generativelanguage.googleapis.com. "
            "AI Studio AQ. keys are currently rejected there. Deploy with Vertex AI instead "
            "(BAYOS_GEMINI_BACKEND=vertex and GOOGLE_CLOUD_PROJECT set)."
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
    title: str = ""
    body: str = ""
    priority: str = "normal"
    tags: List[str] = Field(default_factory=list)
    unread: bool = True
    source: str = "ingest"
    read_at: Optional[str] = None


class InboxPostRequest(BaseModel):
    agent: str = Field(..., min_length=1, description="Registered agent identifier.")
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=50_000)
    priority: str = Field(default="normal", description="low, normal, or high.")
    tags: List[str] = Field(default_factory=list)


class InboxListResponse(BaseModel):
    storage: str
    durable: bool
    cleared_on_scale_to_zero: bool
    notice: str
    total: int
    count: int
    items: List[InboxItem]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp. Naive values are treated as UTC."""
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_tags(tags: List[str]) -> List[str]:
    cleaned: List[str] = []
    seen = set()
    for tag in tags:
        text = tag.strip()
        if not text:
            continue
        if len(text) > 40:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Each tag must be 40 characters or fewer.",
            )
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) > 10:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At most 10 tags are allowed.",
            )
    return cleaned


def item_to_document(item: InboxItem) -> Dict[str, Any]:
    data = item.model_dump()
    data["read_at"] = item.read_at or ""
    data["tags"] = list(item.tags)
    return data


def item_from_document(data: Dict[str, Any], doc_id: str) -> InboxItem:
    payload = dict(data)
    payload["id"] = str(payload.get("id") or doc_id)
    received = payload.get("received_at")
    if isinstance(received, datetime):
        payload["received_at"] = format_utc(received)
    read_at = payload.get("read_at")
    if read_at == "" or read_at is None:
        payload["read_at"] = None
    elif isinstance(read_at, datetime):
        payload["read_at"] = format_utc(read_at)
    tags = payload.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    payload["tags"] = [str(tag) for tag in tags if str(tag).strip()]
    return InboxItem.model_validate(payload)


class InboxPage:
    def __init__(self, storage: str, notice: str, items: List[InboxItem], total: int):
        self.storage = storage
        self.notice = notice
        self.items = items
        self.total = total

    @property
    def durable(self) -> bool:
        return self.storage == "firestore"

    @property
    def cleared_on_scale_to_zero(self) -> bool:
        return self.storage != "firestore"


class InboxStore:
    """Firestore collection `inbox`, with an in-memory deque when that is unreachable."""

    def __init__(self) -> None:
        self._memory: Deque[InboxItem] = deque(maxlen=INBOX_CAPACITY)
        self._lock = threading.Lock()
        self._client: Any = None
        self._override: Optional[str] = None
        self._probe_ok: Optional[bool] = None
        self._probe_until = 0.0
        self._last_error = ""

    def reset_for_tests(self) -> None:
        with self._lock:
            self._memory.clear()
        self._client = None
        self._override = None
        self._probe_ok = None
        self._probe_until = 0.0
        self._last_error = ""

    def attach_client(self, client: Any) -> None:
        """Use a stand-in Firestore client. Tests call this; Cloud Run does not."""
        self._client = client
        self._override = "firestore"
        self._probe_ok = None
        self._probe_until = 0.0
        self._last_error = ""

    def _wanted(self) -> str:
        forced = os.environ.get("BAYOS_INBOX_STORAGE", "").strip().lower()
        if forced == "memory":
            return "memory"
        if self._override == "firestore" or forced == "firestore":
            return "firestore"
        if os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip():
            return "firestore"
        return "memory"

    def _notice(self, storage: str) -> str:
        if storage == "firestore":
            return (
                "Stored in Cloud Firestore (Native mode), collection inbox, in this project. "
                "Items remain when Cloud Run scales to zero."
            )
        text = (
            "In-memory only. This list is wiped when the Cloud Run instance scales to zero, "
            "and separate instances do not share it."
        )
        if self._last_error:
            text += f" Firestore was not reachable ({self._last_error})."
        return text

    def _mark_down(self, exc: Exception) -> None:
        self._probe_ok = False
        self._probe_until = time.time() + 5
        self._last_error = type(exc).__name__
        logger.warning("Firestore inbox unavailable: %s", type(exc).__name__)

    def _client_or_raise(self) -> Any:
        if self._client is not None:
            return self._client
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        if not project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set")
        from google.cloud import firestore

        self._client = firestore.Client(project=project)
        return self._client

    def _probe(self) -> None:
        client = self._client_or_raise()
        query = client.collection(INBOX_COLLECTION).limit(1)
        list(query.stream(timeout=8))
        self._flush_memory()

    def _flush_memory(self) -> None:
        with self._lock:
            pending = list(self._memory)
        if not pending:
            return
        for item in pending:
            self._write_document(item)
        with self._lock:
            self._memory.clear()

    def _write_document(self, item: InboxItem) -> None:
        client = self._client_or_raise()
        client.collection(INBOX_COLLECTION).document(item.id).set(item_to_document(item), timeout=8)

    def _read_document(self, item_id: str) -> Optional[InboxItem]:
        client = self._client_or_raise()
        snapshot = client.collection(INBOX_COLLECTION).document(item_id).get(timeout=8)
        if not getattr(snapshot, "exists", False):
            return None
        return item_from_document(snapshot.to_dict() or {}, snapshot.id)

    def _query_documents(self, since: Optional[datetime], agent: str, unread: Optional[bool]) -> List[InboxItem]:
        from google.cloud import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter

        client = self._client_or_raise()
        query = client.collection(INBOX_COLLECTION).order_by(
            "received_at", direction=firestore.Query.DESCENDING
        )
        if since is not None:
            query = query.where(filter=FieldFilter("received_at", ">", format_utc(since)))
        query = query.limit(INBOX_SCAN_LIMIT)
        found: List[InboxItem] = []
        for snapshot in query.stream(timeout=8):
            item = item_from_document(snapshot.to_dict() or {}, snapshot.id)
            if not self._matches(item, since, agent, unread):
                continue
            found.append(item)
        return found

    def _matches(self, item: InboxItem, since: Optional[datetime], agent: str, unread: Optional[bool]) -> bool:
        if since is not None:
            try:
                received = parse_utc(item.received_at)
            except ValueError:
                return False
            if received <= since:
                return False
        if agent and item.agent != agent:
            return False
        if unread is not None and item.unread != unread:
            return False
        return True

    def _firestore_ready(self) -> bool:
        if self._wanted() != "firestore":
            return False
        now = time.time()
        if self._probe_ok is not None and now < self._probe_until:
            return self._probe_ok
        try:
            self._probe()
        except Exception as exc:
            self._mark_down(exc)
            return False
        self._probe_ok = True
        self._probe_until = time.time() + 60
        self._last_error = ""
        return True

    async def storage_name(self) -> str:
        ready = await asyncio.to_thread(self._firestore_ready)
        return "firestore" if ready else "memory"

    def _memory_upsert(self, item: InboxItem) -> None:
        with self._lock:
            for index, existing in enumerate(self._memory):
                if existing.id == item.id:
                    self._memory[index] = item
                    return
            self._memory.append(item)

    def _memory_get(self, item_id: str) -> Optional[InboxItem]:
        with self._lock:
            for existing in self._memory:
                if existing.id == item_id:
                    return existing
        return None

    def _memory_query(self, since: Optional[datetime], agent: str, unread: Optional[bool]) -> List[InboxItem]:
        with self._lock:
            ordered = list(self._memory)
        ordered.reverse()
        return [item for item in ordered if self._matches(item, since, agent, unread)]

    async def save(self, item: InboxItem) -> str:
        ready = await asyncio.to_thread(self._firestore_ready)
        if ready:
            try:
                await asyncio.to_thread(self._write_document, item)
                return "firestore"
            except Exception as exc:
                self._mark_down(exc)
        self._memory_upsert(item)
        return "memory"

    async def mark_read(self, item_id: str) -> Optional[InboxItem]:
        ready = await asyncio.to_thread(self._firestore_ready)
        if ready:
            try:
                item = await asyncio.to_thread(self._read_document, item_id)
            except Exception as exc:
                self._mark_down(exc)
                item = None
                ready = False
            if ready and item is not None:
                if item.unread:
                    item.unread = False
                    item.read_at = utc_now()
                    try:
                        await asyncio.to_thread(self._write_document, item)
                    except Exception as exc:
                        self._mark_down(exc)
                        self._memory_upsert(item)
                return item
            if ready and item is None and self._memory_get(item_id) is None:
                return None
        item = self._memory_get(item_id)
        if item is None:
            return None
        if item.unread:
            item.unread = False
            item.read_at = utc_now()
            self._memory_upsert(item)
        return item

    async def query(
        self,
        limit: int,
        since: Optional[datetime],
        agent: str,
        unread: Optional[bool],
    ) -> InboxPage:
        ready = await asyncio.to_thread(self._firestore_ready)
        storage = "firestore" if ready else "memory"
        if ready:
            try:
                matched = await asyncio.to_thread(self._query_documents, since, agent, unread)
            except Exception as exc:
                self._mark_down(exc)
                matched = self._memory_query(since, agent, unread)
                storage = "memory"
        else:
            matched = self._memory_query(since, agent, unread)
        selected = matched[:limit]
        return InboxPage(storage, self._notice(storage), selected, len(matched))


inbox_store = InboxStore()


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
    if gemini_backend() == "vertex" and not gemini_project():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vertex AI is not configured. Set GOOGLE_CLOUD_PROJECT, or set GEMINI_API_KEY and BAYOS_GEMINI_BACKEND=apikey.",
        )
    if gemini_backend() == "apikey" and not gemini_api_key():
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
        "gemini_configured": gemini_ready(),
        "gemini_backend": gemini_backend(),
        "gemini_location": gemini_location() if gemini_backend() == "vertex" else "",
        "gemini_model": gemini_model(),
        "inference_mode": inference_mode(),
        "inbox_storage": await inbox_store.storage_name(),
    }


@app.get(
    "/api/v1/inbox",
    response_model=InboxListResponse,
    dependencies=[Depends(verify_auth_key), Depends(check_rate_limit)],
    tags=["Inbox"],
)
async def list_inbox(
    limit: int = Query(default=20, ge=1, le=100),
    since: Optional[str] = Query(
        default=None,
        description="Return items received after this ISO-8601 timestamp. The instant itself is excluded.",
    ),
    agent: Optional[str] = Query(default=None, description="Return items from this agent id."),
    unread: Optional[bool] = Query(default=None, description="true for unread items, false for read items."),
):
    """List inbox items, newest first. Filters support a daily digest watermark."""
    since_dt = None
    if since and since.strip():
        try:
            since_dt = parse_utc(since)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Query since must be an ISO-8601 timestamp, for example 2026-10-11T18:00:00Z.",
            ) from exc
    agent_id = agent.strip().lower() if agent and agent.strip() else ""
    page = await inbox_store.query(limit, since_dt, agent_id, unread)
    return InboxListResponse(
        storage=page.storage,
        durable=page.durable,
        cleared_on_scale_to_zero=page.cleared_on_scale_to_zero,
        notice=page.notice,
        total=page.total,
        count=len(page.items),
        items=page.items,
    )


@app.post(
    "/api/v1/inbox",
    response_model=InboxItem,
    dependencies=[Depends(verify_auth_key), Depends(check_rate_limit)],
    tags=["Inbox"],
)
async def post_inbox(payload: InboxPostRequest):
    """Store an agent result. This does not call Gemini."""
    agent_id = payload.agent.strip().lower()
    if agent_id not in AGENT_PROMPTS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{payload.agent}' not recognized. Registered count: {len(AGENT_PROMPTS)}.",
        )
    title = payload.title.strip()
    body = payload.body.strip()
    if not title or not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Title and body are required.",
        )
    priority = (payload.priority or "normal").strip().lower()
    if priority not in _PRIORITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Priority must be low, normal, or high.",
        )
    tags = clean_tags(payload.tags)
    item = InboxItem(
        id=uuid.uuid4().hex,
        received_at=utc_now(),
        agent=agent_id,
        task=title,
        status="stored",
        output=clip_text(body, MAX_STORED_OUTPUT_CHARS),
        title=title,
        body=clip_text(body, MAX_STORED_OUTPUT_CHARS),
        priority=priority,
        tags=tags,
        unread=True,
        source="post",
    )
    await inbox_store.save(item)
    logger.info("Inbox post id=%s agent=%s priority=%s", item.id, agent_id, priority)
    return item


@app.post(
    "/api/v1/inbox/{item_id}/read",
    response_model=InboxItem,
    dependencies=[Depends(verify_auth_key), Depends(check_rate_limit)],
    tags=["Inbox"],
)
async def mark_inbox_read(item_id: str):
    """Mark one inbox item read. A later unread list will not return it."""
    if not _INBOX_ID_RE.match(item_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inbox id must be 32 hex characters.",
        )
    item = await inbox_store.mark_read(item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inbox item was not found.",
        )
    logger.info("Inbox read id=%s agent=%s", item.id, item.agent)
    return item


@app.post(
    "/api/v1/task/ingest",
    response_model=TaskIngestResponse,
    dependencies=[Depends(verify_auth_key), Depends(check_rate_limit)],
    tags=["Ingestion"],
)
async def ingest_task(payload: TaskIngestRequest):
    """
    Ingests, validates, and routes tasks to target BayOS agents.
    Records the task in the inbox so it can be read back.
    4. Offloads blocking Gemini API calls to an async threadpool.
    """
    agent_id = payload.agent.lower()
    system_instruction = AGENT_PROMPTS.get(agent_id)

    if not system_instruction:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{payload.agent}' not recognized. Registered count: {len(AGENT_PROMPTS)}.",
        )

    task_text = clip_text(payload.task, MAX_STORED_TASK_CHARS)
    record = InboxItem(
        id=uuid.uuid4().hex,
        received_at=utc_now(),
        agent=agent_id,
        task=task_text,
        status="pending",
        output="",
        title=clip_text(payload.task, 200),
        body="",
        source="ingest",
        version="2.4.0",
    )
    await inbox_store.save(record)

    try:
        output_text = await run_inference(agent_id, system_instruction, payload.task)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        record.status = "failed"
        record.output = clip_text(detail, MAX_STORED_OUTPUT_CHARS)
        record.body = record.output
        await inbox_store.save(record)
        logger.info("Ingest id=%s agent=%s status=failed", record.id, agent_id)
        raise

    record.status = "completed"
    record.output = clip_text(output_text, MAX_STORED_OUTPUT_CHARS)
    record.body = record.output
    await inbox_store.save(record)
    logger.info("Ingest id=%s agent=%s status=completed", record.id, agent_id)
    return TaskIngestResponse(
        agent=agent_id,
        status="completed",
        output=output_text,
        version="2.4.0",
    )
