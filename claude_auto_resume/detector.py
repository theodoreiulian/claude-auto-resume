"""
detector.py — Detect Claude Code and Codex limit messages and extract reset times.

Claude Code:
    "You've hit your session limit · resets 5:00 PM EDT"
    "You've hit your session limit · resets 5pm (Eastern Daylight Time)"
    "You've hit your session limit · resets 10:20pm (Europe/Paris)"

The last form is what Claude Code emits under Conductor, which renders the CLI's
stream-json output rather than its terminal UI. It names an IANA zone instead of an
abbreviation, so it can be resolved exactly.

Codex:
    "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro),
     visit https://chatgpt.com/codex/settings/usage to purchase more credits or try
     again at 11:54 PM."
    "... or try again at Sep 10th, 2026 2:22 AM."

Codex always writes the reset in the machine's local time, and only adds the date
when the reset falls on a different day. The wording between the two anchors varies
by plan ("Upgrade to Plus…", "send a request to your admin…"), so only the anchors
are matched.

Extracts the reset time and converts it to a local datetime for scheduling.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Common timezone abbreviations → UTC offset in hours
# We only need approximate accuracy — the +1 minute buffer handles minor drift.
_TZ_OFFSETS: dict[str, float] = {
    # North America
    "EST": -5, "EDT": -4,
    "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6,
    "PST": -8, "PDT": -7,
    "AKST": -9, "AKDT": -8,
    "HST": -10, "HDT": -9,
    # Europe
    "GMT": 0, "BST": 1,
    "WET": 0, "WEST": 1,
    "CET": 1, "CEST": 2,
    "EET": 2, "EEST": 3,
    # Asia/Pacific
    "IST": 5.5,
    "JST": 9, "KST": 9,
    "CST_ASIA": 8,  # China Standard Time (ambiguous with US CST)
    "AEST": 10, "AEDT": 11,
    "ACST": 9.5, "ACDT": 10.5,
    "AWST": 8,
    "NZST": 12, "NZDT": 13,
    # Others
    "UTC": 0,
}


_MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}


@dataclass
class ResetInfo:
    """Information about when a Claude Code or Codex session resets."""
    reset_time: datetime      # Local datetime when the session resets
    resume_time: datetime     # reset_time + 1 minute (when we should send "continue")
    raw_text: str             # The raw matched text from the session
    reset_label: str          # The reset time as written, e.g. "11:54 PM" — identifies the notice


def _parse_time_string(time_str: str) -> Optional[tuple[int, int]]:
    """
    Parse a time string into (hour, minute) in 24-hour format.

    Handles:
        "5:00 PM" → (17, 0)
        "5pm"     → (17, 0)
        "17:00"   → (17, 0)
        "5:30 AM" → (5, 30)
        "12:00 AM"→ (0, 0)
        "12:00 PM"→ (12, 0)
    """
    time_str = time_str.strip()

    # Pattern 1: "5:00 PM" or "5:00PM" or "5:30 am"
    match = re.match(
        r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)",
        time_str,
        re.IGNORECASE,
    )
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        ampm = match.group(3).upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        return (hour, minute)

    # Pattern 2: "5pm" or "5PM" or "5am"
    match = re.match(
        r"(\d{1,2})\s*(AM|PM|am|pm)",
        time_str,
        re.IGNORECASE,
    )
    if match:
        hour = int(match.group(1))
        ampm = match.group(2).upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        return (hour, 0)

    # Pattern 3: "17:00" (24-hour)
    match = re.match(r"(\d{1,2}):(\d{2})$", time_str)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute)

    return None


def _resolve_timezone(tz_str: Optional[str]) -> Optional[timezone | ZoneInfo]:
    """
    Turn a timezone string from the limit message into a tzinfo, or None.

    Handles the two shapes Claude Code produces: an IANA name like "Europe/Paris"
    (Conductor) and an abbreviation like "EDT" (the terminal UI). Anything else —
    a spelled-out name like "Eastern Daylight Time" — yields None, and the caller
    falls back to treating the time as local.
    """
    if not tz_str:
        return None
    tz_str = tz_str.strip()

    # IANA name, e.g. "Europe/Paris". Exact, including the right DST offset for the date.
    if "/" in tz_str:
        try:
            return ZoneInfo(tz_str)
        except (ZoneInfoNotFoundError, ValueError):
            return None

    offset_hours = _TZ_OFFSETS.get(tz_str.upper())
    if offset_hours is None:
        return None
    return timezone(timedelta(hours=offset_hours))


def _resolve_reset_datetime(
    hour: int,
    minute: int,
    tz_str: Optional[str],
) -> datetime:
    """
    Convert a parsed reset time (hour, minute) + optional timezone string
    into a naive local datetime for scheduling.

    Claude Code normally reports the reset in the user's own timezone, in which case
    interpreting it locally and interpreting it in the named zone agree. They diverge
    when the reported zone isn't the machine's — a laptop that travelled, or a zone
    set per-account — and there the named zone is the correct reading.

    If the resulting time is in the past, assume it's tomorrow.
    """
    tz = _resolve_timezone(tz_str)

    if tz is None:
        # No usable zone: read the time as local wall-clock.
        now = datetime.now()
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    now_there = datetime.now(timezone.utc).astimezone(tz)
    candidate = now_there.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_there:
        candidate += timedelta(days=1)

    # Back to a naive local datetime — the rest of the app compares against
    # `datetime.now()`, so everything downstream stays in local wall-clock time.
    return candidate.astimezone().replace(tzinfo=None)


# Claude Code. The · character might be a regular dot, middle dot, or other separator.
_CLAUDE_LIMIT = re.compile(
    r"(?:You've hit your session limit|session limit)"
    r"\s*[·•\-–—]\s*"
    r"resets?\s+"
    r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)"  # Time (group 1)
    r"(?:\s*(?:\(([^)]+)\)|([A-Z]{2,5})))?",      # Timezone in parens (group 2) or abbreviation (group 3)
    re.IGNORECASE,
)

# Codex. Anchors on the opening phrase and "try again at", allowing any wording and any
# line wrapping in between — the Codex TUI wraps the message across several lines. The
# middle stops at the first "try again", so a notice ending "try again later." can't
# borrow the time from a later notice.
_CODEX_LIMIT = re.compile(
    r"hit\s+your\s+usage\s+limit"
    r"(?:(?!try\s+again)[\s\S]){0,600}?"
    r"try\s+again\s+at\s+"
    r"(?P<when>"
    r"(?:(?P<month>[a-z]{3})\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})\s+)?"
    r"(?P<time>\d{1,2}:\d{2}\s*[AP]M))",
    re.IGNORECASE,
)


def _reset_info(reset_time: datetime, match: re.Match, label_group) -> ResetInfo:
    """Build a ResetInfo, labelled by the text from `label_group` to the end of the match."""
    label = match.string[match.start(label_group):match.end()]
    return ResetInfo(
        reset_time=reset_time,
        resume_time=reset_time + timedelta(minutes=1),
        raw_text=match.group(0),
        reset_label=" ".join(label.split()),
    )


def _claude_reset(match: re.Match) -> Optional[ResetInfo]:
    parsed = _parse_time_string(match.group(1))
    if parsed is None:
        return None
    hour, minute = parsed
    tz_str = match.group(2) or match.group(3)  # Either parenthesized or abbreviation
    return _reset_info(_resolve_reset_datetime(hour, minute, tz_str), match, 1)


def _codex_reset(match: re.Match) -> Optional[ResetInfo]:
    parsed = _parse_time_string(match.group("time"))
    if parsed is None:
        return None
    hour, minute = parsed

    if not match.group("year"):
        # Same-day reset, in local time.
        return _reset_info(_resolve_reset_datetime(hour, minute, None), match, "when")

    # A dated reset is exact, so one already in the past is due now rather than
    # rolled forward a day.
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    try:
        reset_time = datetime(int(match.group("year")), month, int(match.group("day")),
                              hour, minute)
    except ValueError:
        return None
    return _reset_info(reset_time, match, "when")


def _last_match(pattern: re.Pattern, content: str) -> Optional[re.Match]:
    match = None
    for match in pattern.finditer(content):
        pass
    return match


def detect_session_limit(content: str) -> Optional[ResetInfo]:
    """
    Scan content for a Claude Code session limit or Codex usage limit message.

    Looks for patterns like:
        "You've hit your session limit · resets 5:00 PM EDT"
        "You've hit your session limit · resets 10:20pm (Europe/Paris)"
        "session limit · resets 5:00 PM"
        "You've hit your usage limit. Upgrade to Pro (…) or try again at 11:54 PM."
        "You've hit your usage limit. … or try again at Sep 10th, 2026 2:22 AM."

    If several notices are present — a terminal screen can show an old one above a
    new one — the last is the one that counts.

    Returns ResetInfo with parsed reset/resume times, or None if not found.
    """
    if not content:
        return None

    candidates = []
    for pattern, resolve in ((_CLAUDE_LIMIT, _claude_reset), (_CODEX_LIMIT, _codex_reset)):
        match = _last_match(pattern, content)
        info = resolve(match) if match else None
        if info is not None:
            candidates.append((match.end(), info))

    if not candidates:
        return None
    return max(candidates, key=lambda c: c[0])[1]
