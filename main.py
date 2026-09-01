
import os
import uuid
import datetime
from typing import Optional, Dict, Any
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

app = FastAPI(title="BayOS Community Ingestion Gateway", version="2.2.0")

# Read API credentials and authentication token from environment
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
BAYOS_AUTH_KEY = os.environ.get("BAYOS_AUTH_KEY", "bayos-open-key-2026")

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

AGENT_PROMPTS = {
    "prof-x-001": (
        "You are POLE (prof-x-001), the central router of BayOS. Analyze the incoming payload and classify "
        "which specialist agent should handle it: [tally-001, scout-001, baymax-001, chronos-001, forge-001, "
        "reverse-001, socrates-001, yoda-001, pony-001]. Return a JSON object with keys 'target_agent' and 'reasoning'."
    ),
    "tally-001": "You are Tally. Extract line items, receipts, and math. Verify arithmetic with exact precision.",
    "scout-001": "You are Scout. Evaluate product specs, deal arbitrage, and resale margins with factual discipline.",
    "baymax-001": "You are Clinical Baymax. Structure clinical study packets and healthcare notes under human oversight.",
    "chronos-001": "You are Chronos. Structure calendar candidates, schedule blocks, and identify collisions.",
    "forge-001": "You are Forge. Design modular system blueprints, agent schemas, and markdown files.",
    "reverse-001": "You are Reverse. Deconstruct workflows from end result to origin using the 9-Point Blueprint.",
    "socrates-001": "You are Socrates. Challenge unexamined premises and ask rigorous first-principle questions.",
    "yoda-001": "You are Yoda. Compress complex inputs into memorable, high-yield, 1-sentence baseline truths.",
    "pony-001": "You are Pony Express. Triage incoming correspondence and draft concise, professional replies."
}

class TaskEnvelope(BaseModel):
    task_id: Optional[str] = Field(default_factory=lambda: f"task-{uuid.uuid4().hex[:8]}")
    source: str = "mobile_shortcut"
    target_agent: Optional[str] = "prof-x-001"
    payload: Dict[str, Any]

@app.get("/healthz")
def health_check():
    return {"status": "HEALTHY", "system": "BayOS Gateway v2.2.0", "timestamp": datetime.datetime.utcnow().isoformat()}

@app.post("/api/v1/task/ingest")
def ingest_task(envelope: TaskEnvelope, x_bayos_key: Optional[str] = Header(None)):
    if x_bayos_key != BAYOS_AUTH_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid BayOS Access Token")

    user_query = envelope.payload.get("query", "")
    agent_id = envelope.target_agent if envelope.target_agent in AGENT_PROMPTS else "prof-x-001"
    system_instruction = AGENT_PROMPTS[agent_id]

    if not client:
        return {
            "status": "QUEUED_DRY_RUN",
            "task_id": envelope.task_id,
            "agent": agent_id,
            "message": "Gateway running in offline test mode. Set GEMINI_API_KEY for live inference.",
            "echo": user_query
        }

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_query,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2
            )
        )
        result_text = response.text.strip()
    except Exception as exc:
        result_text = f"Execution Error: {str(exc)}"

    return {
        "status": "COMPLETED",
        "task_id": envelope.task_id,
        "agent": agent_id,
        "result": result_text,
        "timestamp": datetime.datetime.utcnow().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
