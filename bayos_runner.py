"""Allow-listed Runner recipes.

Recipes live in the Firestore collection `recipes` (memory when Firestore is
off). Approval is a separate authenticated call. Inbox posts and MCP tools
cannot change a recipe's status.
"""

import hashlib
import ipaddress
import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

RECIPE_COLLECTION = "recipes"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ID_RE = re.compile(r"^[a-f0-9]{32}$")
STATUSES = ("draft", "approved", "retired")
HEALTH_FIELDS = (
    "sleep_hours",
    "sleep_start",
    "sleep_end",
    "resting_hr",
    "steps",
    "spo2_min",
    "spo2_avg",
    "notes",
)
RECIPE_TYPES = (
    "health_daily",
    "calendar_read",
    "post_note",
    "notify",
    "reminder_add",
    "open_url",
    "save_vault_note",
)
CREATE_FIELDS = {"name", "version", "type", "params", "schedule_hint"}
RESULT_FIELDS = {"recipe_id", "title", "body", "tags"}
_BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal"}
SEED_NAME = "daily-health"
SEED_ID = hashlib.sha256(b"bayos-recipe:daily-health").hexdigest()[:32]


class RecipeError(ValueError):
    """The recipe body failed the allow-list."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shortcut_path() -> Path:
    return Path(__file__).resolve().parent / "shortcuts" / "runner" / "BayOS-Runner.shortcut"


def _text(value: Any, label: str, limit: int, allow_newline: bool = False) -> str:
    if not isinstance(value, str):
        raise RecipeError(f"{label} must be text.")
    cleaned = value.strip()
    if len(cleaned) > limit:
        raise RecipeError(f"{label} must be {limit} characters or fewer.")
    for char in cleaned:
        code = ord(char)
        if char in ("\n", "\t") and allow_newline:
            continue
        if code < 32 or code == 127:
            raise RecipeError(f"{label} contains a control character.")
    return cleaned


def _whole(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecipeError(f"{label} must be a whole number.")
    if value < low or value > high:
        raise RecipeError(f"{label} must be from {low} to {high}.")
    return value


def _tags(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RecipeError("tags must be a list.")
    cleaned: List[str] = []
    seen = set()
    for tag in value:
        text = _text(tag, "Tag", 40)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) > 10:
            raise RecipeError("At most 10 tags are allowed.")
    return cleaned


def _object(value: Any, allowed: set, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise RecipeError(f"{label} must be a JSON object.")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RecipeError(f"Unknown field '{unknown[0]}'.")
    return value


def open_url_hosts() -> set:
    found = set()
    raw = os.environ.get("BAYOS_OPEN_URL_HOSTS", "")
    for part in raw.split(","):
        host = part.strip().lower().rstrip(".")
        if not host:
            continue
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise RecipeError("An allow-listed host is not a valid name.") from exc
        found.add(host)
    return found


def normalize_open_url(url: str) -> str:
    """https URL whose host is on BAYOS_OPEN_URL_HOSTS. No fetch."""
    raw = _text(url, "url", 500)
    if "\\" in raw:
        raise RecipeError("url must be an https link.")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.lower() != "https":
        raise RecipeError("url must start with https://.")
    if parsed.username or parsed.password:
        raise RecipeError("url must not include a username or password.")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise RecipeError("url must include a host.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise RecipeError("url host is not a valid name.") from exc
    if host in _BLOCKED_HOSTS or host.endswith(".localhost") or host.endswith(".local"):
        raise RecipeError("url host is not allowed.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise RecipeError("url host must be a name, not an IP address.")
    if parsed.port not in (None, 443):
        raise RecipeError("url must use port 443.")
    allowed = open_url_hosts()
    if not allowed:
        raise RecipeError("Set BAYOS_OPEN_URL_HOSTS before saving an open_url recipe.")
    if host not in allowed:
        raise RecipeError("url host is not on the allow list.")
    path = parsed.path or "/"
    return urllib.parse.urlunsplit(("https", host, path, parsed.query, ""))


def validate_params(recipe_type: str, params: Any) -> Dict[str, Any]:
    if recipe_type not in RECIPE_TYPES:
        raise RecipeError(f"Unknown recipe type '{recipe_type}'.")
    if recipe_type == "health_daily":
        body = _object(params, {"fields"}, "params")
        fields = body.get("fields")
        if not isinstance(fields, list) or not fields:
            raise RecipeError("health_daily params.fields must list at least one field.")
        cleaned = []
        for field in fields:
            if not isinstance(field, str) or field not in HEALTH_FIELDS:
                raise RecipeError("health_daily field is not allow-listed.")
            if field not in cleaned:
                cleaned.append(field)
        return {"fields": cleaned}
    if recipe_type == "calendar_read":
        body = _object(params, {"days"}, "params")
        if "days" not in body:
            raise RecipeError("calendar_read requires params.days.")
        return {"days": _whole(body["days"], "days", 1, 60)}
    if recipe_type == "post_note":
        body = _object(params, {"title", "body", "tags"}, "params")
        title = _text(body.get("title", ""), "title", 200)
        note = _text(body.get("body", ""), "body", 8000, allow_newline=True)
        if not title or not note:
            raise RecipeError("post_note requires title and body.")
        return {"title": title, "body": note, "tags": _tags(body.get("tags", []))}
    if recipe_type == "notify":
        body = _object(params, {"text"}, "params")
        text = _text(body.get("text", ""), "text", 500)
        if not text:
            raise RecipeError("notify requires text.")
        return {"text": text}
    if recipe_type == "reminder_add":
        body = _object(params, {"title", "due"}, "params")
        title = _text(body.get("title", ""), "title", 200)
        due = _text(body.get("due", ""), "due", 80)
        if not title or not due:
            raise RecipeError("reminder_add requires title and due.")
        return {"title": title, "due": due}
    if recipe_type == "open_url":
        body = _object(params, {"url"}, "params")
        if "url" not in body:
            raise RecipeError("open_url requires params.url.")
        return {"url": normalize_open_url(body["url"])}
    body = _object(params, {"title", "body"}, "params")
    title = _text(body.get("title", ""), "title", 200)
    note = _text(body.get("body", ""), "body", 8000, allow_newline=True)
    if not title or not note:
        raise RecipeError("save_vault_note requires title and body.")
    return {"title": title, "body": note}


def validate_create(payload: Any) -> Dict[str, Any]:
    body = _object(payload, CREATE_FIELDS, "Recipe")
    for required in ("name", "version", "type", "params"):
        if required not in body:
            raise RecipeError(f"Recipe requires {required}.")
    name = _text(body["name"], "name", 64)
    if not NAME_RE.match(name):
        raise RecipeError("name must be lowercase letters, numbers, and hyphens.")
    version = _whole(body["version"], "version", 1, 1000)
    recipe_type = body["type"]
    if not isinstance(recipe_type, str):
        raise RecipeError("type must be text.")
    params = validate_params(recipe_type, body["params"])
    if "schedule_hint" not in body:
        hint = ""
    else:
        hint = _text(body["schedule_hint"], "schedule_hint", 80)
    return {
        "name": name,
        "version": version,
        "type": recipe_type,
        "params": params,
        "schedule_hint": hint,
    }


def validate_result(payload: Any) -> Dict[str, Any]:
    body = _object(payload, RESULT_FIELDS, "Result")
    for required in ("recipe_id", "title"):
        if required not in body:
            raise RecipeError(f"Result requires {required}.")
    recipe_id = _text(body["recipe_id"], "recipe_id", 32)
    if not ID_RE.match(recipe_id):
        raise RecipeError("recipe_id is not a recipe id.")
    title = _text(body["title"], "title", 200)
    if not title:
        raise RecipeError("title is required.")
    note = ""
    if "body" in body:
        note = _text(body.get("body") or "", "body", 8000, allow_newline=True)
    return {
        "recipe_id": recipe_id,
        "title": title,
        "body": note,
        "tags": _tags(body.get("tags", [])),
    }


def canonical_bytes(record: Dict[str, Any]) -> bytes:
    payload = {
        "id": record["id"],
        "name": record["name"],
        "params": record["params"],
        "schedule_hint": record["schedule_hint"],
        "status": "approved",
        "type": record["type"],
        "version": record["version"],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def canonical_sha256(record: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(record)).hexdigest()


def summary(record: Dict[str, Any]) -> str:
    kind = record["type"]
    params = record["params"]
    hint = record.get("schedule_hint") or ""
    when = f" Suggested time: {hint}." if hint else ""
    if kind == "health_daily":
        fields = ", ".join(params.get("fields") or [])
        return f"Collect {fields} from Apple Health and post them to the hub health log.{when}"
    if kind == "calendar_read":
        return (
            f"Read the next {params.get('days')} days of events from the hub calendar "
            f"and post them to the inbox.{when}"
        )
    if kind == "post_note":
        return f"Post a hub note titled {params.get('title', '')}.{when}"
    if kind == "notify":
        return f"Show a notification on this phone: {params.get('text', '')}.{when}"
    if kind == "reminder_add":
        return f"Add a reminder titled {params.get('title', '')} due {params.get('due', '')}.{when}"
    if kind == "open_url":
        return (
            f"After you confirm, open {params.get('url', '')}. "
            f"The shortcut asks before that link leaves this screen.{when}"
        )
    if kind == "save_vault_note":
        return f"Save a hub note titled {params.get('title', '')} with the tag vault.{when}"
    return "Unknown recipe."


def public_recipe(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": record["id"],
        "name": record["name"],
        "version": record["version"],
        "type": record["type"],
        "params": record["params"],
        "schedule_hint": record["schedule_hint"],
        "status": record["status"],
        "approved_at": record.get("approved_at") or "",
        "sha256": record.get("sha256") or "",
        "summary": summary(record),
    }


def seed_record() -> Dict[str, Any]:
    return {
        "id": SEED_ID,
        "name": SEED_NAME,
        "version": 1,
        "type": "health_daily",
        "params": {"fields": ["sleep_hours", "steps", "resting_hr", "spo2_min", "spo2_avg"]},
        "schedule_hint": "9:00 AM daily",
        "status": "draft",
        "approved_at": "",
        "sha256": "",
    }


def _normalize(data: Dict[str, Any], doc_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    record = {
        "id": str(data.get("id") or doc_id),
        "name": data.get("name"),
        "version": data.get("version"),
        "type": data.get("type"),
        "params": data.get("params"),
        "schedule_hint": data.get("schedule_hint") or "",
        "status": data.get("status"),
        "approved_at": data.get("approved_at") or "",
        "sha256": data.get("sha256") or "",
    }
    approved = record["approved_at"]
    if isinstance(approved, datetime):
        record["approved_at"] = approved.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        record["approved_at"] = str(approved or "")
    if record["status"] not in STATUSES or not isinstance(record["name"], str):
        return None
    try:
        record["version"] = _whole(record["version"], "version", 1, 1000)
        record["params"] = validate_params(str(record["type"]), record["params"])
        record["schedule_hint"] = _text(str(record["schedule_hint"]), "schedule_hint", 80)
    except RecipeError:
        return None
    if not ID_RE.match(record["id"]) or not NAME_RE.match(record["name"]):
        return None
    record["sha256"] = str(record["sha256"] or "")
    return record


class RecipeStore:
    """Firestore collection `recipes`, with an in-memory dict when that is unreachable."""

    def __init__(self) -> None:
        self._memory: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._client: Any = None
        self._override: Optional[str] = None
        self._probe_ok: Optional[bool] = None
        self._probe_until = 0.0
        self._last_error = ""
        self._seed_memory()

    def reset_for_tests(self) -> None:
        with self._lock:
            self._memory.clear()
        self._client = None
        self._override = None
        self._probe_ok = None
        self._probe_until = 0.0
        self._last_error = ""
        self._seed_memory()

    def attach_client(self, client: Any) -> None:
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

    def _client_or_raise(self) -> Any:
        if self._client is not None:
            return self._client
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        if not project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set")
        from google.cloud import firestore

        self._client = firestore.Client(project=project)
        return self._client

    def _backend(self) -> str:
        if self._wanted() != "firestore":
            return "memory"
        now = time.time()
        if self._probe_ok is True and now < self._probe_until:
            return "firestore"
        if self._probe_ok is False and now < self._probe_until:
            return "memory"
        try:
            client = self._client_or_raise()
            list(client.collection(RECIPE_COLLECTION).limit(1).stream(timeout=8))
        except Exception as exc:
            self._probe_ok = False
            self._probe_until = now + 5
            self._last_error = type(exc).__name__
            return "memory"
        self._probe_ok = True
        self._probe_until = now + 30
        self._last_error = ""
        return "firestore"

    def _seed_memory(self) -> None:
        with self._lock:
            if any(item["name"] == SEED_NAME for item in self._memory.values()):
                return
            self._memory[SEED_ID] = seed_record()

    def _write_firestore(self, record: Dict[str, Any]) -> None:
        client = self._client_or_raise()
        client.collection(RECIPE_COLLECTION).document(record["id"]).set(dict(record), timeout=8)

    def _read_firestore(self, recipe_id: str) -> Optional[Dict[str, Any]]:
        client = self._client_or_raise()
        snapshot = client.collection(RECIPE_COLLECTION).document(recipe_id).get(timeout=8)
        if not getattr(snapshot, "exists", False):
            return None
        return _normalize(snapshot.to_dict() or {}, snapshot.id)

    def _list_firestore(self) -> List[Dict[str, Any]]:
        client = self._client_or_raise()
        found = []
        for snapshot in client.collection(RECIPE_COLLECTION).limit(200).stream(timeout=8):
            record = _normalize(snapshot.to_dict() or {}, snapshot.id)
            if record:
                found.append(record)
        return found

    def ensure_seed(self) -> None:
        if self._backend() == "memory":
            self._seed_memory()
            return
        for record in self._list_firestore():
            if record["name"] == SEED_NAME:
                return
        self._write_firestore(seed_record())

    def list_recipes(self, status: str = "", name: str = "") -> List[Dict[str, Any]]:
        if status and status not in STATUSES:
            raise RecipeError("status must be draft, approved, or retired.")
        if name and not NAME_RE.match(name):
            raise RecipeError("name must be lowercase letters, numbers, and hyphens.")
        self.ensure_seed()
        if self._backend() == "firestore":
            rows = self._list_firestore()
        else:
            with self._lock:
                rows = [dict(item) for item in self._memory.values()]
        if status:
            rows = [item for item in rows if item["status"] == status]
        if name:
            rows = [item for item in rows if item["name"] == name]
        rows.sort(key=lambda item: item["name"])
        return rows

    def get(self, recipe_id: str) -> Optional[Dict[str, Any]]:
        if not ID_RE.match(recipe_id or ""):
            return None
        self.ensure_seed()
        if self._backend() == "firestore":
            return self._read_firestore(recipe_id)
        with self._lock:
            found = self._memory.get(recipe_id)
            return dict(found) if found else None

    def create(self, payload: Any) -> Dict[str, Any]:
        draft = validate_create(payload)
        self.ensure_seed()
        if any(item["name"] == draft["name"] for item in self.list_recipes()):
            raise RecipeError(f"A recipe named {draft['name']} already exists.")
        record = {
            "id": uuid.uuid4().hex,
            "name": draft["name"],
            "version": draft["version"],
            "type": draft["type"],
            "params": draft["params"],
            "schedule_hint": draft["schedule_hint"],
            "status": "draft",
            "approved_at": "",
            "sha256": "",
        }
        if self._backend() == "firestore":
            self._write_firestore(record)
        else:
            with self._lock:
                self._memory[record["id"]] = record
        return dict(record)

    def approve(self, recipe_id: str) -> Dict[str, Any]:
        current = self.get(recipe_id)
        if current is None:
            raise KeyError(recipe_id)
        if current["status"] == "approved":
            return current
        if current["status"] != "draft":
            raise RecipeError("Only a draft recipe can be approved.")
        validate_params(current["type"], current["params"])
        current["status"] = "approved"
        current["approved_at"] = utc_now()
        current["sha256"] = canonical_sha256(current)
        if self._backend() == "firestore":
            self._write_firestore(current)
        else:
            with self._lock:
                self._memory[current["id"]] = current
        return dict(current)
