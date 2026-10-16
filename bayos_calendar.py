"""iCloud public-calendar fetch and a small ICS reader.

Times are interpreted in America/Chicago. Floating times (no Z and no TZID)
use that zone as well. The secret calendar URL is never returned to the client.
"""

import ipaddress
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger("bayos.kernel")
CHICAGO = ZoneInfo("America/Chicago")
MAX_CALENDARS = 5
MAX_ICS_BYTES = 1_000_000
MAX_EVENTS = 200
_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


class CalendarUrlError(ValueError):
    """The configured calendar link is not a public https or webcal URL."""


def _resolver(host: str, port: int):
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def normalize_ical_url(url: str, resolve: Optional[Callable] = None) -> str:
    """Return an https URL, or raise CalendarUrlError. Does not fetch it."""
    raw = (url or "").strip()
    if not raw or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise CalendarUrlError("blank")
    parsed = urllib.parse.urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme == "webcal":
        scheme = "https"
    if scheme != "https":
        raise CalendarUrlError("scheme")
    if parsed.username or parsed.password:
        raise CalendarUrlError("userinfo")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host in {"localhost", "metadata", "metadata.google.internal"}:
        raise CalendarUrlError("host")
    port = parsed.port or 443
    if port != 443:
        raise CalendarUrlError("port")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global:
            raise CalendarUrlError("address")
    else:
        lookup = resolve or _resolver
        try:
            infos = lookup(host, port)
        except socket.gaierror as exc:
            raise CalendarUrlError("dns") from exc
        if not infos:
            raise CalendarUrlError("dns")
        for info in infos:
            address = ipaddress.ip_address(info[4][0])
            if not address.is_global:
                raise CalendarUrlError("address")
    path = parsed.path or "/"
    rebuilt = urllib.parse.urlunsplit(("https", parsed.netloc, path, parsed.query, ""))
    return rebuilt


def calendar_urls_from_env(raw: str) -> List[str]:
    found: List[str] = []
    for part in (raw or "").split(","):
        text = part.strip()
        if not text:
            continue
        found.append(text)
        if len(found) >= MAX_CALENDARS:
            break
    return found


def _unfold(text: str) -> List[str]:
    logical: List[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line:
            continue
        if line[0] in (" ", "\t") and logical:
            logical[-1] += line[1:]
        else:
            logical.append(line)
    return logical


def _split_property(line: str):
    colon = line.find(":")
    if colon < 0:
        return None
    left = line[:colon]
    value = line[colon + 1 :]
    pieces = left.split(";")
    name = pieces[0].upper()
    params: Dict[str, str] = {}
    for piece in pieces[1:]:
        if "=" not in piece:
            continue
        key, raw = piece.split("=", 1)
        params[key.upper()] = raw
    return name, params, value


def _parse_rrule(value: str) -> Dict[str, str]:
    rule: Dict[str, str] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        rule[key.upper()] = raw
    return rule


def _parse_when(value: str, params: Dict[str, str]) -> Optional[datetime]:
    text = value.strip()
    if not text:
        return None
    all_day = params.get("VALUE", "").upper() == "DATE" or (len(text) == 8 and "T" not in text)
    if all_day:
        try:
            day = datetime.strptime(text[:8], "%Y%m%d")
        except ValueError:
            return None
        return day.replace(tzinfo=CHICAGO)
    if text.endswith("Z"):
        try:
            parsed = datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return parsed.astimezone(CHICAGO)
    try:
        parsed = datetime.strptime(text[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    tzid = params.get("TZID", "").strip().strip('"')
    if tzid:
        try:
            zone = ZoneInfo(tzid)
        except Exception:
            zone = CHICAGO
        return parsed.replace(tzinfo=zone).astimezone(CHICAGO)
    return parsed.replace(tzinfo=CHICAGO)


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, _month_days(year, month))
    return value.replace(year=year, month=month, day=day)


def _month_days(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    if month in (4, 6, 9, 11):
        return 30
    return 31


def _bydays(rule: Dict[str, str]) -> List[int]:
    raw = rule.get("BYDAY", "")
    found: List[int] = []
    for token in raw.split(","):
        letters = "".join(ch for ch in token.upper() if ch.isalpha())
        if letters in _WEEKDAYS and _WEEKDAYS[letters] not in found:
            found.append(_WEEKDAYS[letters])
    return found


def _in_window(start: datetime, end: datetime, window_start: datetime, window_end: datetime) -> bool:
    if start >= window_end:
        return False
    if end <= window_start and start < window_start:
        return False
    if end == start:
        return window_start <= start < window_end
    return end > window_start and start < window_end


def _expand(
    start: datetime,
    end: datetime,
    rule: Optional[Dict[str, str]],
    window_start: datetime,
    window_end: datetime,
) -> List[Tuple[datetime, datetime]]:
    duration = end - start
    if duration.total_seconds() < 0:
        duration = timedelta(0)
        end = start
    if not rule or not rule.get("FREQ"):
        if _in_window(start, end, window_start, window_end):
            return [(start, end)]
        return []
    freq = rule.get("FREQ", "").upper()
    try:
        interval = max(1, int(rule.get("INTERVAL", "1")))
    except ValueError:
        interval = 1
    count: Optional[int] = None
    if "COUNT" in rule:
        try:
            count = max(0, int(rule["COUNT"]))
        except ValueError:
            count = None
    until: Optional[datetime] = None
    if "UNTIL" in rule:
        until = _parse_when(rule["UNTIL"], {})
    hits: List[Tuple[datetime, datetime]] = []
    emitted = 0

    def accept(occ: datetime) -> bool:
        nonlocal emitted
        if occ < start:
            return True
        emitted += 1
        if count is not None and emitted > count:
            return False
        if until is not None and occ > until:
            return False
        occ_end = occ + duration
        if _in_window(occ, occ_end, window_start, window_end):
            hits.append((occ, occ_end))
        return True

    if freq == "WEEKLY" and _bydays(rule):
        days = _bydays(rule)
        week0 = start - timedelta(days=start.weekday())
        week0 = week0.replace(hour=start.hour, minute=start.minute, second=start.second, microsecond=0)
        for week_index in range(0, 260):
            monday = week0 + timedelta(weeks=week_index * interval)
            if monday > window_end + timedelta(days=7):
                break
            stop = False
            for weekday in days:
                occ = monday + timedelta(days=weekday)
                if not accept(occ):
                    stop = True
                    break
            if stop:
                break
        return hits[:MAX_EVENTS]

    cursor = start
    if freq == "DAILY" and cursor < window_start:
        skipped = int((window_start - cursor).total_seconds() // (86400 * interval))
        if skipped > 1:
            cursor = start + timedelta(days=(skipped - 1) * interval)
            emitted = skipped - 1
    guard = 0
    while guard < 2000 and len(hits) < MAX_EVENTS:
        guard += 1
        if not accept(cursor):
            break
        if cursor > window_end and emitted > 0:
            break
        if freq == "DAILY":
            cursor = cursor + timedelta(days=interval)
        elif freq == "WEEKLY":
            cursor = cursor + timedelta(weeks=interval)
        elif freq == "MONTHLY":
            cursor = _add_months(cursor, interval)
        elif freq == "YEARLY":
            cursor = _add_months(cursor, 12 * interval)
        else:
            break
        if count is not None and emitted >= count:
            break
    return hits


def parse_ics(text: str, now: datetime, days: int) -> List[dict]:
    """Return upcoming events. now may be any aware or naive UTC timestamp."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window_start = now.astimezone(CHICAGO)
    window_end = window_start + timedelta(days=max(1, days))
    events: List[dict] = []
    current: Dict[str, object] = {}
    in_event = False
    for line in _unfold(text):
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            in_event = True
            current = {}
            continue
        if upper == "END:VEVENT":
            if in_event:
                events.extend(_event_occurrences(current, window_start, window_end))
            in_event = False
            current = {}
            continue
        if not in_event:
            continue
        parsed = _split_property(line)
        if not parsed:
            continue
        name, params, value = parsed
        if name in ("DTSTART", "DTEND"):
            current[name] = (value, params)
        elif name in ("SUMMARY", "DESCRIPTION", "LOCATION", "RRULE"):
            current[name] = value
    events.sort(key=lambda item: item["start"])
    return events[:MAX_EVENTS]


def _event_occurrences(current: Dict[str, object], window_start: datetime, window_end: datetime) -> List[dict]:
    start_raw = current.get("DTSTART")
    if not isinstance(start_raw, tuple):
        return []
    start = _parse_when(start_raw[0], start_raw[1])
    if start is None:
        return []
    all_day = len(start_raw[0].strip()) == 8 or start_raw[1].get("VALUE", "").upper() == "DATE"
    end = start
    end_raw = current.get("DTEND")
    if isinstance(end_raw, tuple):
        parsed_end = _parse_when(end_raw[0], end_raw[1])
        if parsed_end is not None:
            end = parsed_end
    elif all_day:
        end = start + timedelta(days=1)
    rule = None
    if isinstance(current.get("RRULE"), str):
        rule = _parse_rrule(str(current["RRULE"]))
    summary = str(current.get("SUMMARY") or "").strip() or "(no title)"
    location = str(current.get("LOCATION") or "").strip()
    details = str(current.get("DESCRIPTION") or "").replace("\\n", "\n").strip()
    if len(details) > 500:
        details = details[:500]
    found: List[dict] = []
    for occ_start, occ_end in _expand(start, end, rule, window_start, window_end):
        found.append(
            {
                "summary": summary,
                "start": occ_start.isoformat(),
                "end": occ_end.isoformat(),
                "all_day": all_day,
                "location": location,
                "details": details,
                "untrusted": True,
            }
        )
    return found


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_ics(url: str, resolve: Optional[Callable] = None) -> str:
    """Fetch one public calendar. Redirects are checked again before they are followed."""
    current = url
    opener = urllib.request.build_opener(_NoRedirect)
    for _hop in range(3):
        target = normalize_ical_url(current, resolve=resolve)
        request = urllib.request.Request(target, headers={"User-Agent": "BayOS-Calendar"}, method="GET")
        try:
            with opener.open(request, timeout=8) as response:
                payload = response.read(MAX_ICS_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location") if exc.headers else None
                if not location:
                    raise CalendarUrlError("redirect") from exc
                current = urllib.parse.urljoin(target, location)
                continue
            raise
        if len(payload) > MAX_ICS_BYTES:
            raise CalendarUrlError("size")
        return payload.decode("utf-8", errors="replace")
    raise CalendarUrlError("redirect")


def upcoming_events(raw_urls: str, now: datetime, days: int, fetch: Optional[Callable[[str], str]] = None) -> dict:
    """Fetch every configured URL. A failure becomes a generic notice, not an exception."""
    urls = calendar_urls_from_env(raw_urls)
    if not urls:
        return {
            "timezone": "America/Chicago",
            "days": days,
            "configured": False,
            "count": 0,
            "notice": "No calendar link is configured.",
            "events": [],
        }
    reader = fetch or fetch_ics
    events: List[dict] = []
    failed = 0
    for url in urls:
        try:
            text = reader(url)
            events.extend(parse_ics(text, now, days))
        except Exception as exc:
            logger.warning("Calendar read failed: %s", type(exc).__name__)
            failed += 1
    events.sort(key=lambda item: item["start"])
    events = events[:MAX_EVENTS]
    notice = "Upcoming events from the public calendar link."
    if failed:
        notice = "One or more calendars could not be read."
    return {
        "timezone": "America/Chicago",
        "days": days,
        "configured": True,
        "count": len(events),
        "notice": notice,
        "events": events,
    }
