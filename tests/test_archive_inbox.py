"""Dry-run selection for the Oct 18 inbox archive. No Firestore client is opened.

Run: python tests/test_archive_inbox.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import archive_oct18_inbox as archive  # noqa: E402


class FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = dict(data)

    def to_dict(self):
        return dict(self._data)

    def set(self, data, merge=False):
        if merge:
            self._data.update(data)
        else:
            self._data = dict(data)


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def stream(self, timeout=None):
        return list(self.docs.values())

    def document(self, doc_id):
        if doc_id not in self.docs:
            self.docs[doc_id] = FakeDoc(doc_id, {})
        return self.docs[doc_id]


class FakeClient:
    def __init__(self, inbox):
        self.collections = {"inbox": FakeCollection(inbox), "inbox_archive": FakeCollection({})}
        self.delete_calls = 0

    def collection(self, name):
        if name not in self.collections:
            self.collections[name] = FakeCollection({})
        return self.collections[name]

    def delete(self, *args, **kwargs):
        self.delete_calls += 1
        raise AssertionError("delete was called")


def note(doc_id, **fields):
    data = {
        "title": "Family dinner",
        "task": "Family dinner",
        "body": "Bring the salad.",
        "output": "",
        "agent": "pony-001",
        "received_at": "2026-10-18T15:00:00Z",
        "tags": ["family"],
    }
    data.update(fields)
    return doc_id, FakeDoc(doc_id, data)


def test_selection_and_apply():
    smoke_id, smoke = note(
        "a" * 32,
        title="Deploy smoke note",
        task="Deploy smoke note",
        body="Inbox stored without Gemini.",
        agent="tally-001",
    )
    pong_id, pong = note(
        "b" * 32,
        title="Reply with the single word pong.",
        task="Reply with the single word pong.",
        body="pong",
        agent="prof-x-001",
        received_at=datetime(2026, 10, 18, 18, 4, tzinfo=timezone.utc),
    )
    tagged_id, tagged = note(
        "c" * 32,
        title="Check",
        body="ordinary words",
        tags=["deploy-check"],
        agent="forge-001",
    )
    real_id, real = note("d" * 32, title="Fund the four walls", body="Food and shelter first.", agent="tally-001")
    other_day_id, other_day = note(
        "e" * 32,
        title="Deploy smoke note",
        body="Inbox stored without Gemini.",
        received_at="2026-10-17T23:59:59Z",
    )
    already_id, already = note(
        "f" * 32,
        title="Deploy smoke note",
        body="Inbox stored without Gemini.",
        archived=True,
    )
    arm_id, arm = note(
        "1" * 32,
        title="Arm chatgpt-001",
        body="Stored from the arm test.",
        agent="chatgpt-001",
    )
    client = FakeClient(
        {
            smoke_id: smoke,
            pong_id: pong,
            tagged_id: tagged,
            real_id: real,
            other_day_id: other_day,
            already_id: already,
            arm_id: arm,
        }
    )
    calls = []

    def factory(project):
        calls.append(project)
        return client

    code = archive.main(["--project", "example-project"], client_factory=factory)
    assert code == 0, code
    assert calls == ["example-project"]
    assert client.delete_calls == 0
    assert smoke.to_dict().get("archived") is not True
    assert "inbox_archive" in client.collections
    assert client.collections["inbox_archive"].docs == {}

    refused = archive.main(["--project", "example-project", "--apply"], client_factory=factory)
    assert refused == 2
    assert smoke.to_dict().get("archived") is not True

    applied = archive.main(
        ["--project", "example-project", "--apply", "--confirm", "archive-oct-18"],
        client_factory=factory,
    )
    assert applied == 0
    archived_ids = set(client.collections["inbox_archive"].docs)
    assert archived_ids == {smoke_id, pong_id, tagged_id, arm_id}
    for doc_id in archived_ids:
        original = client.collections["inbox"].docs[doc_id].to_dict()
        copy = client.collections["inbox_archive"].docs[doc_id].to_dict()
        assert original["archived"] is True
        assert original["title"]
        assert copy["archived_from"] == "inbox"
        assert copy["id"] == doc_id
        assert copy["body"] == client.collections["inbox"].docs[doc_id].to_dict()["body"]
    assert real.to_dict().get("archived") is not True
    assert other_day.to_dict().get("archived") is not True
    assert client.delete_calls == 0

    source = (ROOT / "scripts" / "archive_oct18_inbox.py").read_text(encoding="utf-8")
    assert ".delete(" not in source
    assert "delete_document" not in source
    bare = archive.main([])
    assert bare == 2
    print("archive inbox ok")


if __name__ == "__main__":
    test_selection_and_apply()
