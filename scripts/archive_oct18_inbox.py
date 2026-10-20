"""Archive 2026-10-18 deploy-check and test inbox notes.

Default is a dry run. The script copies each match into the inbox_archive
collection and sets archived=true on the original inbox document. It never
deletes a document.

There is no default project. Pass --project yourself. A write also requires
--apply --confirm archive-oct-18. Do not point --project at production until
a dry run has been reviewed, and do not apply until the gateway that hides
archived notes is the one serving that project. This file contains no project
id, key, or credential.
"""

import argparse
import sys
from datetime import datetime, timezone

INBOX_COLLECTION = "inbox"
ARCHIVE_COLLECTION = "inbox_archive"
TARGET_DAY = "2026-10-18"
CONFIRM = "archive-oct-18"

# Substrings, compared after case folding, against title, task, body, and output.
# A tag equal to deploy-check also matches. A bare "test" tag does not.
MARKERS = (
    "deploy smoke",
    "deploy-check",
    "deploy check",
    "reply with the single word pong",
    "inbox stored without gemini",
    "stored from the arm test",
    "smoke-test",
    "smoke test",
)


def received_on_day(value, day: str) -> bool:
    if isinstance(value, datetime):
        stamp = value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return stamp.startswith(day)
    if isinstance(value, str):
        return value.startswith(day)
    return False


def note_text(doc: dict) -> str:
    parts = []
    for key in ("title", "task", "body", "output"):
        value = doc.get(key) or ""
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts).casefold()


def tags_of(doc: dict):
    tags = doc.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        return []
    return [str(tag).strip().casefold() for tag in tags if str(tag).strip()]


def is_deploy_check_or_test(doc: dict, day: str = TARGET_DAY) -> bool:
    """True for an active deploy-check or test note received on day."""
    if not isinstance(doc, dict):
        return False
    if doc.get("archived") is True:
        return False
    if not received_on_day(doc.get("received_at"), day):
        return False
    if "deploy-check" in tags_of(doc):
        return True
    text = note_text(doc)
    return any(marker in text for marker in MARKERS)


def matching_documents(client, day: str):
    found = []
    for snapshot in client.collection(INBOX_COLLECTION).stream(timeout=30):
        data = snapshot.to_dict() or {}
        if is_deploy_check_or_test(data, day):
            found.append((snapshot.id, data))
    found.sort(key=lambda pair: (str(pair[1].get("received_at") or ""), pair[0]))
    return found


def archive_fields(doc_id: str, doc: dict, now: str) -> dict:
    archived = dict(doc)
    archived["id"] = doc_id
    archived["archived_from"] = INBOX_COLLECTION
    archived["archived_at"] = now
    archived["archived"] = True
    return archived


def apply_archive(client, matches, now: str) -> int:
    """Copy each match, then mark the original. Never deletes."""
    for doc_id, doc in matches:
        payload = archive_fields(doc_id, doc, now)
        client.collection(ARCHIVE_COLLECTION).document(doc_id).set(payload)
        client.collection(INBOX_COLLECTION).document(doc_id).set(
            {"archived": True, "archived_at": now},
            merge=True,
        )
    return len(matches)


def format_report(matches, applied: bool) -> str:
    verb = "Archived" if applied else "Would archive"
    lines = [
        f"{verb} {len(matches)} inbox note(s).",
        "Originals stay in the inbox collection with archived=true.",
        "Copies are written to inbox_archive. Nothing is deleted.",
    ]
    for doc_id, doc in matches:
        title = doc.get("title") or doc.get("task") or ""
        if not isinstance(title, str):
            title = ""
        title = " ".join(title.split())
        if len(title) > 80:
            title = title[:77] + "..."
        agent = doc.get("agent") or ""
        received = doc.get("received_at") or ""
        lines.append(f"{doc_id} agent={agent} received_at={received} title={title}")
    return "\n".join(lines)


def live_client(project: str):
    from google.cloud import firestore

    return firestore.Client(project=project)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive Oct 18 deploy-check and test inbox notes.")
    parser.add_argument("--project", default="", help="GCP project id. Required. There is no default.")
    parser.add_argument("--day", default=TARGET_DAY, help="UTC day prefix, default 2026-10-18.")
    parser.add_argument("--apply", action="store_true", help="Write the archive. Omit this for a dry run.")
    parser.add_argument("--confirm", default="", help=f"Required with --apply. Must be {CONFIRM}.")
    return parser


def main(argv=None, client_factory=None) -> int:
    args = build_parser().parse_args(argv)
    project = args.project.strip()
    if not project:
        print(
            "Pass --project. This script has no default project and does not contact Firestore until you do.",
            file=sys.stderr,
        )
        return 2
    if args.apply and args.confirm != CONFIRM:
        print(
            f"Refusing to write. Review a dry run, then re-run with --apply --confirm {CONFIRM}.",
            file=sys.stderr,
        )
        return 2
    factory = client_factory or live_client
    client = factory(project)
    matches = matching_documents(client, args.day.strip())
    if not args.apply:
        print(format_report(matches, applied=False))
        print("Dry run. No documents were written.")
        return 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    count = apply_archive(client, matches, now)
    print(format_report(matches, applied=True))
    print(f"Wrote {count} archive copy(s) and updated the originals. No document was deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
