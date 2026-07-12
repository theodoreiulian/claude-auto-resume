"""
detector.py — Detect Claude Code session limit messages and extract reset times.

Parses messages like:
    "You've hit your session limit · resets 5:00 PM EDT"
    "You've hit your session limit · resets 5pm (Eastern Daylight Time)"

Extracts the reset time and converts it to a local datetime for scheduling.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


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


@dataclass
class ResetInfo:
    """Information about when a Claude Code session resets."""
    reset_time: datetime      # Local datetime when the session resets
    resume_time: datetime     # reset_time + 1 minute (when we should send "continue")
    raw_text: str             # The raw matched text from the terminal


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


def _resolve_reset_datetime(
    hour: int,
    minute: int,
    tz_str: Optional[str],
) -> datetime:
    """
    Convert a parsed reset time (hour, minute) + optional timezone string
    into a local datetime object.

    If the resulting time is in the past, assume it's tomorrow.
    """
    now = datetime.now()

    # Build a candidate datetime for today
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If a timezone abbreviation was provided, we may need to adjust.
    # However, Claude Code typically shows the time in the user's local timezone,
    # so we treat it as local time unless we have reason not to.
    # The timezone is displayed for informational purposes.

    # If the time is in the past, it means the reset is tomorrow
    if candidate <= now:
        candidate += timedelta(days=1)

    return candidate


def detect_session_limit(content: str) -> Optional[ResetInfo]:
    """
    Scan terminal content for Claude Code's session limit message.

    Looks for patterns like:
        "You've hit your session limit · resets 5:00 PM EDT"
        "You've hit your session limit · resets 5pm (Eastern Daylight Time)"
        "session limit · resets 5:00 PM"

    Returns ResetInfo with parsed reset/resume times, or None if not found.
    """
    if not content:
        return None

    # Primary pattern: look for the session limit message with reset time
    # The · character might be a regular dot, middle dot, or other separator
    pattern = (
        r"(?:You've hit your session limit|session limit)"
        r"\s*[·•\-–—]\s*"
        r"resets?\s+"
        r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)"  # Time (group 1)
        r"(?:\s*(?:\(([^)]+)\)|([A-Z]{2,5})))?"       # Timezone in parens (group 2) or abbreviation (group 3)
    )

    match = re.search(pattern, content, re.IGNORECASE)
    if not match:
        return None

    time_str = match.group(1)
    tz_str = match.group(2) or match.group(3)  # Either parenthesized or abbreviation

    parsed = _parse_time_string(time_str)
    if parsed is None:
        return None

    hour, minute = parsed
    reset_time = _resolve_reset_datetime(hour, minute, tz_str)
    resume_time = reset_time + timedelta(minutes=1)

    return ResetInfo(
        reset_time=reset_time,
        resume_time=resume_time,
        raw_text=match.group(0),
    )
