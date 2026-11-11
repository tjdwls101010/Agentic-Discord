"""Snowflakes and times: Discord ids encode their creation time, and every bound a user gives becomes one."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from discord.errors import Refusal

EPOCH_MS = 1420070400000
_RELATIVE = re.compile(r"^(\d+)\s*([dhm])$")
_UNITS = {"d": "days", "h": "hours", "m": "minutes"}
_LINK = re.compile(r"discord(?:app)?\.com/channels/(@me|\d+)(?:/(\d+))?(?:/(\d+))?")


def time_of(snowflake: int) -> datetime:
    return datetime.fromtimestamp(((snowflake >> 22) + EPOCH_MS) / 1000, tz=timezone.utc)


def at(moment: datetime) -> int:
    """The smallest snowflake of `moment`'s millisecond (no real id is below it for that time)."""
    ms = int(moment.timestamp() * 1000) - EPOCH_MS
    return max(ms, 0) << 22


def iso(moment: datetime) -> str:
    """The archive's time format: UTC with microseconds and a Z."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_discord_time(text: str | None) -> str | None:
    """Discord's `2026-01-02T03:04:05.678000+00:00` in the archive's format."""
    if not text:
        return None
    return iso(datetime.fromisoformat(text.replace("Z", "+00:00")))


def parse_link(text: str) -> tuple[str | None, int | None, int | None] | None:
    """(server id or '@me', channel id, message id) from a Discord link, or None if `text` is not one."""
    m = _LINK.search(text)
    if not m:
        return None
    server = m.group(1)
    return server, int(m.group(2)) if m.group(2) else None, int(m.group(3)) if m.group(3) else None


def parse_bound(text: str, *, now: datetime | None = None) -> int:
    """A snowflake for a range bound: a message id (digits, 15 or more) or a message link, `7d`/`12h`/`30m` ago, or an ISO date or date-time in the local time zone (a date alone is local midnight)."""
    text = text.strip()
    now = now or datetime.now().astimezone()
    if text.isdigit():
        if len(text) >= 15:
            return int(text)
        raise Refusal(f"{text!r} is too short to be a message id (ids have 15 or more digits)",
                      fix="give a message id or link, 7d/12h/30m, or a date like 2026-11-01 or 2026-11-01T09:30", kind="usage")
    link = parse_link(text)
    if link and link[2]:
        return link[2]
    m = _RELATIVE.match(text)
    if m:
        return at(now - timedelta(**{_UNITS[m.group(2)]: int(m.group(1))}))
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise Refusal(f"cannot read {text!r} as a time or message id",
                      fix="give a message id or link, 7d/12h/30m, or a date like 2026-11-01 or 2026-11-01T09:30", kind="usage") from None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return at(moment)
