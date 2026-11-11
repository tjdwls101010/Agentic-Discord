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
    reaches_head: bool


def prove(page: Page) -> tuple[Proof | None, bool]:
    """The range `page` read through, or None when it proves none; and whether the walk reached the channel's newest message.

    The second value can be true with no range: an oldest-first page that came back empty proves nothing new is after `after`.
    """
    ids = [m.id for m in page.messages]
    short = len(page.received_ids) < page.requested
    if page.around:
        return (Proof(min(ids), max(ids), False) if ids else None), False
    if page.after is not None:
        crossed = page.before is not None and any(i >= page.before for i in page.received_ids)
        lo = page.after + 1
        if crossed or (page.before is not None and page.before_is_past and short):
            hi = page.before - 1
        elif ids:
            hi = max(ids)
        else:
            return None, short and page.before is None
        head = short and not crossed
        return (Proof(lo, hi, head) if lo <= hi else None), head
    # newest first: `before` is the cursor sent, or absent for the newest page
    lo = page.channel_id if short else (min(ids) if ids else None)
    if page.before is not None and page.before_is_past:
        hi = page.before - 1
    elif ids:
        hi = max(ids)
    else:
        return None, False
    head = page.before is None and bool(ids)
    if lo is None or lo > hi:
        return None, False
    return Proof(lo, hi, head), head
