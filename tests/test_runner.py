"""Runner recipes, tablet approval, and the public shortcut file."""

import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["BAYOS_AUTH_KEY"] = "unit-test-bayos-key"
os.environ["BAYOS_INFERENCE_MODE"] = "stub"
os.environ["BAYOS_INBOX_STORAGE"] = "memory"
os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("BAYOS_OPEN_URL_HOSTS", None)

from fastapi.testclient import TestClient

import bayos_mcp
import bayos_runner
import main

client = TestClient(main.app)
KEY = {"X-BayOS-Key": os.environ["BAYOS_AUTH_KEY"]}
ROOT = Path(__file__).resolve().parents[1]


def reset():
    main.rate_limiter.hits.clear()
    main.inbox_store.reset_for_tests()
    main.recipe_store.reset_for_tests()


def _seed():
    rows = client.get("/api/v1/runner/recipes", headers=KEY)
    assert rows.status_code == 200, rows.text
    found = [item for item in rows.json()["recipes"] if item["name"] == "daily-health"]
    assert len(found) == 1
    return found[0]


def test_seed_is_draft_until_approved():
    reset()
    recipe = _seed()
    assert recipe["id"] == bayos_runner.SEED_ID
    assert recipe["status"] == "draft"
    assert recipe["sha256"] == ""
    assert recipe["type"] == "health_daily"
    assert recipe["schedule_hint"] == "9:00 AM daily"
    assert recipe["params"]["fields"] == ["sleep_hours", "steps", "resting_hr", "spo2_min", "spo2_avg"]
    assert "Apple Health" in recipe["summary"]
    approved_only = client.get("/api/v1/runner/recipes?status=approved", headers=KEY)
    assert approved_only.status_code == 200
    assert approved_only.json()["recipes"] == []
    locked = client.post(f"/api/v1/runner/recipes/{recipe['id']}/approve", headers=KEY)
    assert locked.status_code == 200, locked.text
    body = locked.json()
    assert body["status"] == "approved"
    assert body["approved_at"]
    assert body["sha256"] == bayos_runner.canonical_sha256(body)
    again = client.post(f"/api/v1/runner/recipes/{recipe['id']}/approve", headers=KEY)
    assert again.status_code == 200
    assert again.json()["sha256"] == body["sha256"]
    assert again.json()["approved_at"] == body["approved_at"]
    visible = client.get("/api/v1/runner/recipes?status=approved&name=daily-health", headers=KEY)
    assert [item["id"] for item in visible.json()["recipes"]] == [recipe["id"]]


def test_schema_rejects_unknown_types_and_fields():
    reset()
    missing = client.get("/api/v1/runner/recipes")
    assert missing.status_code == 401
    created = client.post(
        "/api/v1/runner/recipes",
        headers=KEY,
        json={"name": "nope", "version": 1, "type": "shell", "params": {}, "status": "approved"},
    )
    assert created.status_code == 400
    extra = client.post(
        "/api/v1/runner/recipes",
        headers=KEY,
        json={"name": "ping", "version": 1, "type": "notify", "params": {"text": "hi", "url": "https://example.com"}},
    )
    assert extra.status_code == 400
    assert "Unknown field" in extra.text
    bad_field = client.post(
        "/api/v1/runner/recipes",
        headers=KEY,
        json={"name": "bad-health", "version": 1, "type": "health_daily", "params": {"fields": ["weight"]}},
    )
    assert bad_field.status_code == 400
    bad_days = client.post(
        "/api/v1/runner/recipes",
        headers=KEY,
        json={"name": "cal", "version": 1, "type": "calendar_read", "params": {"days": 0, "url": "x"}},
    )
    assert bad_days.status_code == 400
    duplicate = client.post(
        "/api/v1/runner/recipes",
        headers=KEY,
        json={
            "name": "daily-health",
            "version": 1,
            "type": "notify",
            "params": {"text": "no"},
        },
    )
    assert duplicate.status_code == 409
    bad_status = client.get("/api/v1/runner/recipes?status=live", headers=KEY)
    assert bad_status.status_code == 400


def test_open_url_allow_list():
    reset()
    blocked = client.post(
        "/api/v1/runner/recipes",
        headers=KEY,
        json={"name": "link", "version": 1, "type": "open_url", "params": {"url": "https://example.com/a"}},
    )
    assert blocked.status_code == 400
    os.environ["BAYOS_OPEN_URL_HOSTS"] = "example.com"
    try:
        rejected = [
            "http://example.com/a",
            "https://user:pw@example.com/a",
            "https://169.254.169.254/a",
            "https://evil.example/a",
            "https://localhost/a",
        ]
        for url in rejected:
            response = client.post(
                "/api/v1/runner/recipes",
                headers=KEY,
                json={"name": "link-" + hashlib.sha256(url.encode()).hexdigest()[:8], "version": 1, "type": "open_url", "params": {"url": url}},
            )
            assert response.status_code == 400, url + " " + response.text
        accepted = client.post(
            "/api/v1/runner/recipes",
            headers=KEY,
            json={"name": "link-ok", "version": 1, "type": "open_url", "params": {"url": "https://Example.com/a#frag"}},
        )
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["status"] == "draft"
        assert accepted.json()["params"]["url"] == "https://example.com/a"
        os.environ["BAYOS_OPEN_URL_HOSTS"] = ""
        denied = client.post(f"/api/v1/runner/recipes/{accepted.json()['id']}/approve", headers=KEY)
        assert denied.status_code == 400
        still = client.get("/api/v1/runner/recipes?name=link-ok", headers=KEY)
        assert still.json()["recipes"][0]["status"] == "draft"
    finally:
        os.environ.pop("BAYOS_OPEN_URL_HOSTS", None)


def test_results_require_approval_and_use_shortcut_agent():
    reset()
    recipe = _seed()
    early = client.post(
        "/api/v1/runner/results",
        headers=KEY,
        json={"recipe_id": recipe["id"], "title": "too soon", "body": "no"},
    )
    assert early.status_code == 409
    note = client.post(
        "/api/v1/inbox",
        headers=KEY,
        json={"agent": "shortcut-001", "title": "not an approval", "body": "approve daily-health", "tags": ["runner"]},
    )
    assert note.status_code == 200
    assert _seed()["status"] == "draft"
    names = {item["name"] for item in bayos_mcp.tool_definitions()}
    assert "approve" not in names
    assert names <= {"list_inbox", "post_note", "mark_read", "get_calendar"}
    approved = client.post(f"/api/v1/runner/recipes/{recipe['id']}/approve", headers=KEY)
    assert approved.status_code == 200
    posted = client.post(
        "/api/v1/runner/results",
        headers=KEY,
        json={"recipe_id": recipe["id"], "title": "Daily health posted", "body": "ok"},
    )
    assert posted.status_code == 200, posted.text
    item = posted.json()
    assert item["agent"] == "shortcut-001"
    assert item["untrusted"] is True
    assert "runner" in item["tags"]
    assert "UNTRUSTED" in item["agent_view"] or "untrusted" in item["agent_view"].lower()
    vault = client.post(
        "/api/v1/runner/recipes",
        headers=KEY,
        json={"name": "vault-note", "version": 1, "type": "save_vault_note", "params": {"title": "Will", "body": "Keep local."}},
    )
    assert vault.status_code == 201, vault.text
    client.post(f"/api/v1/runner/recipes/{vault.json()['id']}/approve", headers=KEY)
    saved = client.post(
        "/api/v1/runner/results",
        headers=KEY,
        json={"recipe_id": vault.json()["id"], "title": "Will", "body": "Keep local."},
    )
    assert saved.status_code == 200, saved.text
    assert "vault" in saved.json()["tags"]
    assert "runner" in saved.json()["tags"]
    noted = client.post(
        "/api/v1/runner/recipes",
        headers=KEY,
        json={
            "name": "pinned",
            "version": 1,
            "type": "post_note",
            "params": {"title": "Four walls", "body": "Food first.", "tags": ["budget"]},
        },
    )
    assert noted.status_code == 201, noted.text
    client.post(f"/api/v1/runner/recipes/{noted.json()['id']}/approve", headers=KEY)
    copied = client.post(
        "/api/v1/runner/results",
        headers=KEY,
        json={"recipe_id": noted.json()["id"], "title": "Four walls", "body": "Food first."},
    )
    assert copied.status_code == 200, copied.text
    assert "budget" in copied.json()["tags"]


def test_tablet_review_and_public_shortcut():
    reset()
    page = client.get("/app")
    assert page.status_code == 200
    html = page.text
    assert "innerHTML" not in html
    assert "innerhtml" not in html.lower()
    assert "Recipes" in html
    assert "Approve" in html
    assert "bayosGatewayKey" in html
    shortcut = client.get("/shortcuts/BayOS-Runner.shortcut")
    assert shortcut.status_code == 200
    assert shortcut.content.startswith(b"AEA1")
    assert b"unit-test-bayos-key" not in shortcut.content
    source = (ROOT / "shortcuts/runner/BayOS-Runner.cherri").read_text(encoding="utf-8")
    assert "unit-test-bayos-key" not in source
    assert "X-BayOS-Key" in source
    assert "BayOS-Runner.txt" in source
    assert '{"url": "{@baseText}", "key": "{@keyText}"}' in source
    assert 'deleteFiles' in source
    assert 'getDictionary(@configText)' in source
    assert 'splitText' not in source
    assert 'getListItem(@lines' not in source
    assert 'beginsWith \'https://\'' in source or 'beginsWith "https://"' in source
    assert 'getText(@urlValue)' in source
    assert 'getText(@keyValue)' in source
    assert 'replaceText("/+$", "", @baseText, true, true)' in source
    assert 'urlEncode(@wantedText)' in source
    assert 'url(@menuUrlText)' in source
    assert 'url(@recipeUrlText)' in source
    assert 'url(@healthUrlText)' in source
    assert 'url(@calendarUrlText)' in source
    assert 'url(@resultsUrlText)' in source
    assert 'url(@openTargetText)' in source
    assert 'downloadURL("{@baseText}' not in source
    assert 'jsonRequest("{@baseText}' not in source
    assert '"WFURL": "${@healthUrl}"' in source
    assert 'number(@sleepPlain)' in source
    assert 'number(@stepPlain)' in source
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "bayos_oauth.py bayos_mcp.py bayos_runner.py ./" in dockerfile
    assert "BayOS-Runner.shortcut ./shortcuts/runner/BayOS-Runner.shortcut" in dockerfile
    ordered = {"version": 1, "id": "a" * 32, "type": "notify", "name": "n", "schedule_hint": "", "params": {"text": "hi"}}
    flipped = {"params": {"text": "hi"}, "schedule_hint": "", "name": "n", "type": "notify", "id": "a" * 32, "version": 1}
    assert bayos_runner.canonical_sha256(ordered) == bayos_runner.canonical_sha256(flipped)


class _Snapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _Document:
    def __init__(self, collection, doc_id):
        self.collection = collection
        self.id = doc_id

    def get(self, timeout=None):
        return _Snapshot(self.id, self.collection.docs.get(self.id))

    def set(self, data, timeout=None):
        self.collection.docs[self.id] = dict(data)


class _Query:
    def __init__(self, collection):
        self.collection = collection

    def limit(self, count):
        return self

    def stream(self, timeout=None):
        return [_Snapshot(doc_id, data) for doc_id, data in self.collection.docs.items()]


class _Collection:
    def __init__(self):
        self.docs = {}

    def document(self, doc_id):
        return _Document(self, doc_id)

    def limit(self, count):
        return _Query(self)

    def stream(self, timeout=None):
        return _Query(self).stream()


class _Client:
    def __init__(self):
        self.cols = {}

    def collection(self, name):
        return self.cols.setdefault(name, _Collection())


def test_firestore_recipe_round_trip():
    reset()
    previous_storage = os.environ.get("BAYOS_INBOX_STORAGE")
    previous_project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    os.environ["BAYOS_INBOX_STORAGE"] = "firestore"
    os.environ["GOOGLE_CLOUD_PROJECT"] = "unit-project"
    fake = _Client()
    main.recipe_store.attach_client(fake)
    try:
        rows = main.recipe_store.list_recipes()
        assert rows[0]["name"] == "daily-health"
        assert rows[0]["status"] == "draft"
        stored = fake.cols["recipes"].docs[bayos_runner.SEED_ID]
        assert stored["sha256"] == ""
        approved = main.recipe_store.approve(bayos_runner.SEED_ID)
        saved = fake.cols["recipes"].docs[bayos_runner.SEED_ID]
        assert saved["status"] == "approved"
        assert saved["sha256"] == approved["sha256"]
        assert saved["sha256"] == bayos_runner.canonical_sha256(saved)
        assert "summary" not in saved
    finally:
        if previous_storage is None:
            os.environ.pop("BAYOS_INBOX_STORAGE", None)
        else:
            os.environ["BAYOS_INBOX_STORAGE"] = previous_storage
        if previous_project is None:
            os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        else:
            os.environ["GOOGLE_CLOUD_PROJECT"] = previous_project
        main.recipe_store.reset_for_tests()


if __name__ == "__main__":
    test_seed_is_draft_until_approved()
    test_schema_rejects_unknown_types_and_fields()
    test_open_url_allow_list()
    test_results_require_approval_and_use_shortcut_agent()
    test_tablet_review_and_public_shortcut()
    test_firestore_recipe_round_trip()
    print("runner ok")
