"""Turn real captures into anonymous fixtures (maintainers only).

    python3 tests/support/scrub.py

Reads `.claude/skills/discord/data/captures/*.json` (from tests/live/capture.py) and writes `tests/support/fixtures/<name>.json` plus `fixtures/expected.json`.

- Ids: every snowflake in a value, a path, a mention token or a URL is replaced by a new one, in the same order: the n-th smallest id becomes the n-th id of a made-up sequence, so sorting, range bounds and "same id" relations (a thread and the message it was made from) survive while no real id does.
- Text: every other string becomes synthetic text, except closed sets (content types, locales) and times. A message's mention tokens are kept, with their ids replaced, so fixtures still exercise naming. Times in a message become the new id's time.
- Expectations are computed from the captures before scrubbing and carried through the same id map, so a test comparing a fixture's output with them is not comparing the scrubber with itself.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import SKILL_DIR  # run as a script, this file's folder is first on the import path

HERE = Path(__file__).resolve().parent
CAPTURES = SKILL_DIR / "data" / "captures"
FIXTURES = HERE / "fixtures"

EPOCH_MS = 1420070400000
BASE = datetime(2030, 1, 1, tzinfo=timezone.utc)
ID = re.compile(r"(?<!\d)\d{15,20}(?!\d)")
#: Keys whose digit strings are bit fields, not ids.
BITS = {"permissions", "allow", "deny", "flags", "permissions_new", "system_channel_flags"}
#: Keys whose strings are kept: closed sets and things that identify nobody.
KEEP = {"content_type", "locale", "preferred_locale", "sort_by", "sort_order", "archived", "with_counts", "limit", "offset",
        "before", "after", "max_id", "min_id", "type"}
TIME_KEYS = re.compile(r"(timestamp|_at|expiry)$")
#: Sizes that would fingerprint a server even without its name.
SIZES = {"approximate_member_count", "approximate_presence_count", "member_count", "premium_subscription_count",
         "max_members", "max_presence_count"}


def collect_ids(x, key: str | None = None, out: set | None = None) -> set[int]:
    out = set() if out is None else out
    if isinstance(x, dict):
        for k, v in x.items():
            collect_ids(v, k, out)
    elif isinstance(x, list):
        for v in x:
            collect_ids(v, key, out)
    elif isinstance(x, str) and key not in BITS:
        out.update(int(m) for m in ID.findall(x))
    elif isinstance(x, int) and key not in BITS and x >= 10**15:
        out.add(x)
    return out


def snowflake(moment: datetime, seq: int) -> int:
    return ((int(moment.timestamp() * 1000) - EPOCH_MS) << 22) | (seq & 0xFFF)


class Scrubber:
    def __init__(self, ids: set[int]) -> None:
        self.map = {old: snowflake(BASE + timedelta(minutes=n), n) for n, old in enumerate(sorted(ids))}
        self.texts: dict[str, str] = {}

    def id(self, old: int) -> int:
        return self.map[old]

    def ids_in(self, text: str) -> str:
        return ID.sub(lambda m: str(self.map.get(int(m.group()), m.group())), text)

    def time_of(self, new_id: int) -> str:
        return datetime.fromtimestamp(((new_id >> 22) + EPOCH_MS) / 1000, tz=timezone.utc).isoformat()

    def text(self, old: str, kind: str) -> str:
        if old not in self.texts:
            self.texts[old] = f"anon-{len(self.texts) + 1}"
        return self.texts[old]

    def content(self, old: str) -> str:
        """Synthetic words, with the original's mention and emoji tokens kept (ids replaced)."""
        tokens = re.findall(r"<(?:@!?|#|@&|a?:\w+:)\d+>", old)
        tokens = [re.sub(r"<(a?):\w+:", r"<\1:emoji:", t) for t in tokens]
        return " ".join([self.text(old, "text")] + [self.ids_in(t) for t in tokens]) if old else ""

    def scrub(self, x, key: str | None = None, owner_id: int | None = None):
        if isinstance(x, dict):
            own = None
            if "id" in x and "timestamp" in x:
                own = self.id(int(x["id"]))
            return {k: self.scrub(v, k, own if own is not None else owner_id) for k, v in x.items()}
        if isinstance(x, list):
            return [self.scrub(v, key, owner_id) for v in x]
        if isinstance(x, bool) or x is None or isinstance(x, float):
            return x
        if isinstance(x, int):
            if key in SIZES:
                return 0
            return self.map.get(x, x) if key not in BITS else x
        if not isinstance(x, str):
            return x
        if key in BITS or key in KEEP:
            return x
        if ID.fullmatch(x):
            return str(self.map[int(x)])
        if key and TIME_KEYS.search(key):
            return self.time_of(owner_id) if owner_id else BASE.isoformat()
        if key == "content":
            return self.content(x)
        if key in ("url", "proxy_url", "icon_url", "proxy_icon_url") or x.startswith("http"):
            return f"https://example.invalid/{self.text(x, 'url')}"
        return self.text(x, key or "text")


def expectations(captures: dict[str, dict], s: Scrubber) -> dict:
    """What the tests expect, computed from the real captures, with ids carried through the map."""
    out: dict = {}
    for name in ("chat_page", "qna_page", "thread_start"):
        if name in captures:
            msgs = captures[name]["body"]
            out[name] = {
                "ids_oldest_first": sorted(s.id(int(m["id"])) for m in msgs),
                "replies_with_target": sum(1 for m in msgs if m["type"] == 19 and m.get("referenced_message")),
                "replies_target_deleted": sum(1 for m in msgs if m["type"] == 19 and "referenced_message" in m and m["referenced_message"] is None),
                "thread_starters": sum(1 for m in msgs if m["type"] == 21),
                "threads_created": sum(1 for m in msgs if m["type"] == 18),
                "with_files": sum(1 for m in msgs if m.get("attachments")),
            }
    if "forum_threads" in captures:
        b = captures["forum_threads"]["body"]
        out["forum_threads"] = {"ids": sorted(s.id(int(t["id"])) for t in b["threads"]), "total": b.get("total_results")}
    if "search" in captures:
        b = captures["search"]["body"]
        out["search"] = {"total": b["total_results"], "ids": [s.id(int(g[0]["id"])) for g in b["messages"]]}
    # Measured on the real account the day these were captured: `channels` listed 54 and counted 316 hidden, and two of the hidden answered 403 code 50001.
    out["guild_channels"] = {"visible": 54, "hidden": 316}
    return out


def main() -> int:
    files = sorted(CAPTURES.glob("*.json"))
    if not files:
        print(f"no captures in {CAPTURES}; run tests/live/capture.py first", file=sys.stderr)
        return 1
    captures = {f.stem: json.loads(f.read_text()) for f in files}
    ids: set[int] = set()
    for c in captures.values():
        collect_ids(c["body"], None, ids)
        ids.update(int(m) for m in ID.findall(c["path"]))
        collect_ids(c.get("query") or {}, None, ids)
    s = Scrubber(ids)
    FIXTURES.mkdir(exist_ok=True)
    for name, c in captures.items():
        fixture = {"path": s.ids_in(c["path"]), "query": {k: s.ids_in(v) for k, v in (c.get("query") or {}).items()},
                   "status": c["status"], "body": s.scrub(c["body"])}
        (FIXTURES / f"{name}.json").write_text(json.dumps(fixture, ensure_ascii=False, indent=1) + "\n")
    (FIXTURES / "expected.json").write_text(json.dumps(expectations(captures, s), indent=1) + "\n")
    print(f"wrote {len(captures)} fixtures to {FIXTURES}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
