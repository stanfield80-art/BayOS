"""Arms: GPT schema, tablet page, health ingest, calendar parser, registered senders.

Run: python tests/test_arms.py
Env is set before main is imported. Inbox stays in memory. Gemini is not called.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["BAYOS_AUTH_KEY"] = "test-gateway-key"
os.environ["BAYOS_INFERENCE_MODE"] = "stub"
os.environ["BAYOS_INBOX_STORAGE"] = "memory"
os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
os.environ.pop("BAYOS_ENABLE_DOCS", None)
os.environ.pop("ICAL_URLS", None)

from datetime import datetime, timezone  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import bayos_calendar  # noqa: E402
import main  # noqa: E402

KEY = {"X-BayOS-Key": "test-gateway-key"}
failures = []


def check(name, ok, detail=""):
    if ok:
        print("PASS", name)
    else:
        print("FAIL", name, detail)
        failures.append(name)


def reset():
    main.rate_limiter.hits.clear()
    main.inbox_store.reset_for_tests()


def public_resolver(host, port):
    return [(None, None, None, None, ("93.184.216.34", port))]


def private_resolver(host, port):
    return [(None, None, None, None, ("10.1.1.1", port))]


def test_health_and_docs():
    reset()
    client = TestClient(main.app)
    health = client.get("/health")
    check("health 200", health.status_code == 200, str(health.status_code))
    check("health keys", set(health.json().keys()) == {"status", "version", "inbox_storage"}, str(health.json()))
    check("docs off", client.get("/docs").status_code == 404)
    check("openapi off", client.get("/openapi.json").status_code == 404)


def test_gpt_schema():
    reset()
    client = TestClient(main.app)
    denied = client.get("/gpt/openapi.json")
    check("schema is public", denied.status_code == 200, str(denied.status_code))
    response = client.get(
        "/gpt/openapi.json",
        headers={"x-forwarded-proto": "https", "x-forwarded-host": "bayos.example"},
    )
    body = response.text
    lowered = body.lower()
    document = response.json()
    check("openapi 3.1", document.get("openapi") == "3.1.0", document.get("openapi"))
    check("server from request", document["servers"][0]["url"] == "https://bayos.example")
    check("paths only inbox", set(document["paths"]) == {"/api/v1/inbox", "/api/v1/inbox/{id}/read"})
    check("list and post", set(document["paths"]["/api/v1/inbox"]) == {"get", "post"})
    check("mark read", set(document["paths"]["/api/v1/inbox/{id}/read"]) == {"post"})
    scheme = document["components"]["securitySchemes"]["BayOSKey"]
    check("api key header", scheme == {"type": "apiKey", "in": "header", "name": "X-BayOS-Key"}, str(scheme))
    params = [item["name"] for item in document["paths"]["/api/v1/inbox"]["get"]["parameters"]]
    check("list filters", params == ["unread", "since", "agent", "limit"], str(params))
    for banned in ("firestore", "google_cloud", "/health", "/calendar", "health/daily", "vertex", "secret"):
        check("schema hides " + banned, banned not in lowered, banned)


def test_agents_and_health_daily():
    reset()
    client = TestClient(main.app)
    for agent in ("chatgpt-001", "gemini-001", "shortcut-001", "tablet-001", "claude-001", "joseph"):
        posted = client.post(
            "/api/v1/inbox",
            headers=KEY,
            json={"agent": agent, "title": "Arm " + agent, "body": "Stored from the arm test."},
        )
        check("agent " + agent, posted.status_code == 200 and posted.json()["agent"] == agent, posted.text[:300])
        check("untrusted " + agent, posted.json().get("untrusted") is True)

    open_post = client.post("/api/v1/health/daily", json={"date": "2026-10-12"})
    check("health daily requires key", open_post.status_code == 401, str(open_post.status_code))
    missing = client.post("/api/v1/health/daily", headers=KEY, json={})
    check("health daily needs date", missing.status_code == 422, str(missing.status_code))
    bad = client.post("/api/v1/health/daily", headers=KEY, json={"date": "2026-13-40"})
    check("health daily rejects calendar date", bad.status_code == 400, bad.text[:200])

    low_oxygen = client.post(
        "/api/v1/health/daily",
        headers=KEY,
        json={"date": "2026-10-12", "spo2_min": 89, "sleep_hours": 7},
    )
    low_sleep = client.post(
        "/api/v1/health/daily",
        headers=KEY,
        json={"date": "2026-10-11", "sleep_hours": 4.9, "spo2_min": 96},
    )
    boundary = client.post(
        "/api/v1/health/daily",
        headers=KEY,
        json={
            "date": "2026-10-10",
            "sleep_hours": 5,
            "spo2_min": 90,
            "spo2_avg": 96,
            "steps": 1000,
            "resting_hr": 60,
            "sleep_start": "22:00",
            "sleep_end": "03:00",
            "notes": "ok",
        },
    )
    check("spo2 89 is high", low_oxygen.status_code == 200 and low_oxygen.json()["priority"] == "high", low_oxygen.text[:300])
    check("sleep 4.9 is high", low_sleep.status_code == 200 and low_sleep.json()["priority"] == "high", low_sleep.text[:300])
    item = boundary.json()
    check("boundary is normal", boundary.status_code == 200 and item["priority"] == "normal", boundary.text[:400])
    check("health tags", item.get("tags") == ["health", "sleep"], str(item.get("tags")))
    check("health title", item.get("title") == "Daily health 2026-10-10", item.get("title"))
    check("health agent", item.get("agent") == "shortcut-001")
    check("health untrusted", item.get("untrusted") is True)
    check("health body date", "Date: 2026-10-10" in item.get("body", ""))
    check("health body steps", "Steps: 1000" in item.get("body", ""))
    bare = client.post("/api/v1/health/daily", headers=KEY, json={"date": "2026-10-09"})
    check("empty metrics stay normal", bare.status_code == 200 and bare.json()["priority"] == "normal", bare.text[:300])


def test_tablet():
    reset()
    client = TestClient(main.app)
    page = client.get("/app")
    check("tablet 200", page.status_code == 200, str(page.status_code))
    check("tablet html", "text/html" in page.headers.get("content-type", ""))
    csp = page.headers.get("content-security-policy", "")
    check("csp default none", "default-src 'none'" in csp, csp)
    html = page.text
    nonce = ""
    marker = 'nonce="'
    start = html.find(marker)
    if start >= 0:
        start += len(marker)
        nonce = html[start : html.find('"', start)]
    check("csp nonce matches", nonce and ("script-src 'nonce-" + nonce + "'") in csp and ("style-src 'nonce-" + nonce + "'") in csp, csp)
    lowered = html.lower()
    check("no innerHTML", "innerhtml" not in lowered)
    for host in ("googleapis", "cloudflare", "unpkg", "jsdelivr"):
        check("no " + host, host not in lowered)
    check("forget key", "Forget key" in html)
    check("local storage key", "bayosGatewayKey" in html)
    check("tablet agent", "tablet-001" in html and "joseph" in html)
    manifest = client.get("/app/manifest.webmanifest")
    check("manifest", manifest.status_code == 200 and manifest.json()["start_url"] == "/app")
    icon = client.get("/app/icon-192.png")
    check("icon png", icon.status_code == 200 and icon.content.startswith(b"\x89PNG"))
    icon512 = client.get("/app/icon-512.png")
    check("icon 512", icon512.status_code == 200 and icon512.content.startswith(b"\x89PNG"))


def test_calendar_urls():
    blocked = [
        "http://calendar.example/a",
        "file:///tmp/a.ics",
        "https://127.0.0.1/a",
        "https://169.254.169.254/a",
        "https://10.0.0.1/a",
        "https://localhost/a",
    ]
    for url in blocked:
        try:
            bayos_calendar.normalize_ical_url(url, resolve=public_resolver)
            check("blocked " + url, False, "accepted")
        except bayos_calendar.CalendarUrlError:
            check("blocked " + url, True)
    try:
        bayos_calendar.normalize_ical_url("https://calendar.example/a", resolve=private_resolver)
        check("dns private blocked", False, "accepted")
    except bayos_calendar.CalendarUrlError:
        check("dns private blocked", True)
    rewritten = bayos_calendar.normalize_ical_url("webcal://calendar.example/a", resolve=public_resolver)
    check("webcal becomes https", rewritten == "https://calendar.example/a", rewritten)


ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=America/Chicago:20261012T110000
DTEND;TZID=America/Chicago:20261012T120000
SUMMARY:Folded 
 title
END:VEVENT
BEGIN:VEVENT
DTSTART:20261013T150000
DTEND:20261013T160000
SUMMARY:Floating
END:VEVENT
BEGIN:VEVENT
DTSTART:20261013T200000Z
DTEND:20261013T210000Z
SUMMARY:From Zulu
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261014
DTEND;VALUE=DATE:20261015
SUMMARY:All day
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/Chicago:20261001T090000
DTEND;TZID=America/Chicago:20261001T093000
RRULE:FREQ=DAILY
SUMMARY:Daily meds
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/Chicago:20261005T140000
DTEND;TZID=America/Chicago:20261005T150000
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Monday review
END:VEVENT
END:VCALENDAR
"""


def test_calendar_parse():
    now = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    events = bayos_calendar.parse_ics(ICS, now, 14)
    by_summary = {}
    for event in events:
        by_summary.setdefault(event["summary"], []).append(event)
    folded = by_summary.get("Folded title", [])
    check("folded summary", len(folded) == 1 and folded[0]["start"].startswith("2026-10-12T11:00:00-05:00"), str(folded))
    floating = by_summary.get("Floating", [])
    check("floating chicago", len(floating) == 1 and floating[0]["start"].startswith("2026-10-13T15:00:00-05:00"), str(floating))
    zulu = by_summary.get("From Zulu", [])
    check("zulu chicago", len(zulu) == 1 and zulu[0]["start"].startswith("2026-10-13T15:00:00-05:00"), str(zulu))
    allday = by_summary.get("All day", [])
    check("all day", len(allday) == 1 and allday[0]["all_day"] is True, str(allday))
    daily = by_summary.get("Daily meds", [])
    daily_days = sorted(item["start"][:10] for item in daily)
    check("daily starts on the 13th", daily_days and daily_days[0] == "2026-10-13", str(daily_days[:5]))
    check("daily stays in window", daily_days and daily_days[-1] <= "2026-10-26", str(daily_days[-3:]))
    mondays = sorted(item["start"][:10] for item in by_summary.get("Monday review", []))
    check("weekly mondays", mondays == ["2026-10-12", "2026-10-19"], str(mondays))
    check("events untrusted", all(item["untrusted"] is True for item in events))

    long_ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=America/Chicago:20200101T090000
DTEND;TZID=America/Chicago:20200101T093000
RRULE:FREQ=DAILY
SUMMARY:Old daily
END:VEVENT
END:VCALENDAR
"""
    long_events = bayos_calendar.parse_ics(long_ics, now, 1)
    check("old daily reaches window", any(item["start"].startswith("2026-10-13T09:00:00") for item in long_events), str(long_events[:3]))


def test_calendar_route():
    reset()
    client = TestClient(main.app)
    closed = client.get("/api/v1/calendar")
    check("calendar requires key", closed.status_code == 401, str(closed.status_code))
    os.environ.pop("ICAL_URLS", None)
    empty = client.get("/api/v1/calendar", headers=KEY)
    payload = empty.json()
    check("calendar unset", empty.status_code == 200 and payload["configured"] is False and payload["events"] == [], empty.text[:300])
    check("calendar notice", payload.get("notice") == "No calendar link is configured.")

    secret = "https://calendar.example/private-token-do-not-leak"
    os.environ["ICAL_URLS"] = secret

    def fake_fetch(url):
        if url != secret:
            raise AssertionError(url)
        return ICS

    original = bayos_calendar.fetch_ics
    bayos_calendar.fetch_ics = fake_fetch
    try:
        listed = client.get("/api/v1/calendar?days=14", headers=KEY)
    finally:
        bayos_calendar.fetch_ics = original
        os.environ.pop("ICAL_URLS", None)
    text = listed.text
    check("calendar listed", listed.status_code == 200 and listed.json()["configured"] is True, text[:400])
    check("calendar hides url", "private-token" not in text and "calendar.example" not in text, text[:400])
    check("days bounds", client.get("/api/v1/calendar?days=0", headers=KEY).status_code == 422)
    check("days upper", client.get("/api/v1/calendar?days=61", headers=KEY).status_code == 422)

    def boom(url):
        raise bayos_calendar.CalendarUrlError("scheme")

    os.environ["ICAL_URLS"] = secret
    bayos_calendar.fetch_ics = boom
    try:
        failed = bayos_calendar.upcoming_events(secret, datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc), 14)
    finally:
        bayos_calendar.fetch_ics = original
        os.environ.pop("ICAL_URLS", None)
    check("failed calendar hides url", "private-token" not in str(failed) and failed["events"] == [], str(failed))


def test_health_notes_follow_arm_clearance():
    reset()
    client = TestClient(main.app)
    plain = client.post(
        "/api/v1/inbox",
        headers=KEY,
        json={"agent": "tally-001", "title": "Four walls", "body": "Food and shelter first.", "tags": ["budget"]},
    )
    health = client.post(
        "/api/v1/inbox",
        headers=KEY,
        json={"agent": "shortcut-001", "title": "Daily health 2026-10-18", "body": "Sleep hours: 7", "tags": ["Health", "sleep"]},
    )
    clinical = client.post(
        "/api/v1/inbox",
        headers=KEY,
        json={"agent": "baymax-001", "title": "Study block", "body": "Read the chapter.", "tags": ["clinical"]},
    )
    check("posted notes", plain.status_code == 200 and health.status_code == 200 and clinical.status_code == 200, health.text[:300])
    health_id = health.json()["id"]
    stamps = {
        "Four walls": "2026-10-18T15:00:00Z",
        "Daily health 2026-10-18": "2026-10-18T16:00:00Z",
        "Study block": "2026-10-18T17:00:00Z",
    }
    for item in main.inbox_store._memory:
        if item.title in stamps:
            item.received_at = stamps[item.title]

    def titles(response):
        check("list 200", response.status_code == 200, response.text[:300])
        return [item["title"] for item in response.json()["items"]]

    operator = titles(client.get("/api/v1/inbox?limit=20", headers=KEY))
    check("operator sees health", operator == ["Study block", "Daily health 2026-10-18", "Four walls"], str(operator))
    check("operator total", client.get("/api/v1/inbox?limit=20", headers=KEY).json()["total"] == 3)

    hidden = titles(client.get("/api/v1/inbox?limit=1&viewer=chatgpt-001", headers=KEY))
    check("chatgpt skips newer health notes", hidden == ["Four walls"], str(hidden))
    for arm in ("claude-001", "gemini-001", "tablet-001", "dojo-001", "not-an-arm"):
        seen = titles(client.get("/api/v1/inbox?limit=20&viewer=" + arm, headers=KEY))
        check(arm + " hides health", seen == ["Four walls"], str(seen))
    for arm in ("prof-x-001", "baymax-001", "shortcut-001", "joseph"):
        seen = titles(client.get("/api/v1/inbox?limit=1&viewer=" + arm, headers=KEY))
        check(arm + " first page is the newest health note", seen == ["Study block"], str(seen))
        full = titles(client.get("/api/v1/inbox?limit=20&viewer=" + arm, headers=KEY))
        check(arm + " sees all three", len(full) == 3, str(full))

    check("prof-x sensitivity", main.ARM_SENSITIVITY["prof-x-001"] == frozenset({"health"}))
    check("one gemini prompt", sum(1 for key in main.AGENT_PROMPTS if key == "gemini-001") == 1)

    stored = None
    for item in main.inbox_store._memory:
        if item.id == health_id:
            stored = item
    check("found stored health", stored is not None)
    stored.archived = True
    after = titles(client.get("/api/v1/inbox?limit=20", headers=KEY))
    check("archived health is hidden", "Daily health 2026-10-18" not in after and "Four walls" in after, str(after))
    stored.archived = False


def main_test():
    test_health_and_docs()
    test_gpt_schema()
    test_agents_and_health_daily()
    test_health_notes_follow_arm_clearance()
    test_tablet()
    test_calendar_urls()
    test_calendar_parse()
    test_calendar_route()
    print("FAILURES", len(failures))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main_test()
