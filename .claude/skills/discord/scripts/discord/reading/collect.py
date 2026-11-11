"""The collector: walking a channel's messages page by page in one direction, storing each page with what it proves.

Discord pages at most 100 messages, newest first, and ignores `before` when `after` is sent, so a range with both bounds is walked oldest first from `after` and stopped at `before` here.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from discord import archive, discord_api
from discord.errors import Refusal
from discord.records import Message

#: How far behind the head --new starts: ids from different Discord servers are not issued in strict time order.
NEW_OVERLAP = timedelta(seconds=5)
#: How old a bound must be, by the local clock, to count as past when the channel's last message does not show it.
PAST_MARGIN = timedelta(seconds=60)


@dataclass(frozen=True)
class Spec:
    """A range request. `mode` is last, after, before, around or new; `after`/`before` are exclusive snowflakes."""
    mode: str
    limit: int
    after: int | None = None
    before: int | None = None
    around: int | None = None
    skip_covered: bool = False


@dataclass
class Outcome:
    messages: list[Message] = field(default_factory=list)   # what the transcript shows, oldest first
    fetched: int = 0                                         # how many of them came from Discord in this call
    from_archive: int = 0                                    # how many came from ranges skipped with skip_covered
    requests: int = 0
    cut: str | None = None                                   # which side the limit cut: "newer" or "older"
    resume: dict = field(default_factory=dict)               # the arguments that continue a cut range
    reached_start: bool = False
    reached_head: bool = False


def _progress(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def collect(api, db, channel_id: int, spec: Spec, *, last_message_id: int | None = None, now=None) -> Outcome:
    """Fetch `spec`'s range of `channel_id` into the archive, one transaction per page. The channel's row must already be stored.

    `last_message_id` is what a listing reported, used to tell that a `before` lies in the past; `now` returns the current UTC datetime (for tests).
    """
    now = now or (lambda: datetime.now(timezone.utc))
    out = Outcome()

    def is_past(bound: int | None) -> bool:
        if bound is None:
            return False
        if last_message_id is not None and bound <= last_message_id:
            return True
        return bound <= discord_api.at(now() - PAST_MARGIN)

    def stamp() -> str:
        return discord_api.iso(now())

    def fetch(**kw) -> tuple[list[Message], set[int], str]:
        at = stamp()
        msgs, ids = api.messages(channel_id, **kw)
        out.requests += 1
        return msgs, ids, at

    if spec.mode == "around":
        n = min(spec.limit, discord_api.PAGE)
        msgs, ids, at = fetch(around=spec.around, limit=n)
        db.record_page(archive.Page(channel_id=channel_id, messages=tuple(msgs), fetched_at=at, requested=n,
                                    received_ids=frozenset(ids), around=True))
        out.messages, out.fetched = msgs, len(msgs)
        if msgs:
            out.resume = {"older": {"before": msgs[0].id}, "newer": {"after": msgs[-1].id}}
        return out

    if spec.mode in ("after", "new"):
        after, before = spec.after, spec.before
        known: set[int] = set()
        if spec.mode == "new":
            head = db.head(channel_id)
            if head is None:
                raise Refusal("this archive has never reached the newest message of this channel, so --new has no starting point",
                              fix="read it once with --last N (or any read that reaches the newest message), then use --new")
            after = max(0, discord_api.at(discord_api.time_of(head.hi) - NEW_OVERLAP) - 1)
            known = {int(r["id"]) for r in db.stored_raw(channel_id, after + 1, head.hi)}
            new_from = head.hi
        past = is_past(before)
        cursor = after
        while True:
            if spec.skip_covered:
                r = next((r for r in db.coverage(channel_id) if r.lo <= cursor + 1 <= r.hi), None)
                if r is not None:
                    top = r.hi if before is None else min(r.hi, before - 1)
                    stored = [discord_api.parse_message(x) for x in db.stored_raw(channel_id, cursor + 1, top)]
                    out.messages += stored
                    out.from_archive += len(stored)
                    cursor = r.hi
                    if before is not None and cursor >= before - 1:
                        break
                    continue
            want = min(discord_api.PAGE, spec.limit - out.fetched)
            if want <= 0:
                out.cut = "newer"
                out.resume = {"after": cursor, **({"before": before} if before is not None else {})}
                break
            msgs, ids, at = fetch(after=cursor, limit=want)
            keep = [m for m in msgs if before is None or m.id < before]
            db.record_page(archive.Page(channel_id=channel_id, messages=tuple(keep), fetched_at=at, requested=want,
                                        received_ids=frozenset(ids), after=cursor, before=before, before_is_past=past))
            out.messages += keep
            out.fetched += len(keep)
            if len(ids) < want or len(keep) < len(msgs):
                out.reached_head = len(ids) < want and len(keep) == len(msgs)
                break
            cursor = max(ids)
            _progress(f"fetched {out.fetched} messages…")
        if spec.mode == "new":
            out.messages = [m for m in out.messages if m.id > new_from or m.id not in known]
        return out

    # newest first: --last, --before alone, or the default
    cursor = spec.before
    past = is_past(cursor)
    pages: list[list[Message]] = []
    while True:
        if spec.skip_covered and cursor is not None:
            r = next((r for r in db.coverage(channel_id) if r.lo <= cursor - 1 <= r.hi), None)
            if r is not None:
                stored = [discord_api.parse_message(x) for x in db.stored_raw(channel_id, r.lo, cursor - 1)]
                pages.append(stored)
                out.from_archive += len(stored)
                cursor, past = r.lo, True
                if r.lo <= channel_id:
                    out.reached_start = True
                    break
                continue
        want = min(discord_api.PAGE, spec.limit - out.fetched)
        if want <= 0:
            out.cut = "older"
            out.resume = {"before": cursor}
            break
        msgs, ids, at = fetch(**({"before": cursor} if cursor is not None else {}), limit=want)
        db.record_page(archive.Page(channel_id=channel_id, messages=tuple(msgs), fetched_at=at, requested=want,
                                    received_ids=frozenset(ids), before=cursor, before_is_past=past))
        if cursor is None:
            out.reached_head = True
        pages.append(msgs)
        out.fetched += len(msgs)
        if len(ids) < want:
            out.reached_start = True
            break
        cursor, past = min(ids), True
        _progress(f"fetched {out.fetched} messages…")
    out.messages = [m for page in reversed(pages) for m in page]
    return out
