"""What one fetched page proves about a channel: the id range read through, and whether it reached the newest message."""

from __future__ import annotations

from dataclasses import dataclass

from discord.records import Message


@dataclass(frozen=True)
class Page:
    """One response to a channel's message listing, described by the request that produced it.

    `after` and `before` are the exclusive bounds of the walk this page belongs to: for an oldest-first walk `after` is the cursor sent to Discord and `before` the point where the walk stops (never sent with it); for a newest-first walk `before` is the cursor sent. `received_ids` are the ids Discord returned, before the collector dropped any at or past `before`, so a page shorter than `requested` means the channel ran out in the direction of the walk. `before_is_past` says `before` was observed to be in the past (at or below the channel's last message, or older than the local clock less a margin), which is what lets an empty answer prove a range.
    """
    channel_id: int
    messages: tuple[Message, ...]
    fetched_at: str
    requested: int
    received_ids: frozenset[int]
    after: int | None = None
    before: int | None = None
    before_is_past: bool = False
    around: bool = False


@dataclass(frozen=True)
class Proof:
    lo: int
    hi: int


def prove(page: Page) -> tuple[Proof | None, int | None]:
    """The range `page` read through, or None when it proves none; and, when the walk reached the channel's newest message, the newest id it found (where `read --new` continues), else None.

    The second value can come with no range: an oldest-first page that came back empty proves nothing is newer than `after`, and returns `after`.
    """
    ids = [m.id for m in page.messages]
    short = len(page.received_ids) < page.requested
    if page.around:
        return (Proof(min(ids), max(ids)) if ids else None), None
    if page.after is not None:
        crossed = page.before is not None and any(i >= page.before for i in page.received_ids)
        lo = page.after + 1
        if crossed or (page.before is not None and page.before_is_past and short):
            hi = page.before - 1
        elif ids:
            hi = max(ids)
        else:
            return None, (page.after if short and page.before is None else None)
        if lo > hi:
            return None, None
        # Reached the end: the newest message found, or, with none found, the bottom of the range (nothing newer was there).
        head = (max(ids) if ids else lo) if short and not crossed else None
        return Proof(lo, hi), head
    # newest first: `before` is the cursor sent, or absent for the newest page
    lo = page.channel_id if short else (min(ids) if ids else None)
    if page.before is not None and page.before_is_past:
        hi = page.before - 1
    elif ids:
        hi = max(ids)
    else:
        return None, None
    if lo is None or lo > hi:
        return None, None
    return Proof(lo, hi), (max(ids) if page.before is None and ids else None)
