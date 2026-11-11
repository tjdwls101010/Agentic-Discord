"""Capture real API responses for fixtures (maintainers only; uses the token in scripts/.env).

    python3 tests/live/capture.py

Writes one JSON file per response to `.claude/skills/discord/data/captures/` (gitignored): {"path", "query", "status", "body"}. Then `python3 tests/support/scrub.py` turns them into anonymous fixtures under tests/support/fixtures/. Keep the set small: every request goes out under the user's account.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # tests/ on the path for support.harness, as `python3 -m unittest` puts it

from support.harness import SKILL_DIR, engine  # noqa: E402

credentials = engine("discord.credentials")
api = engine("discord.discord_api")

OUT = SKILL_DIR / "data" / "captures"

#: (name, path, query). Chosen for variety: a server's roles, your roles and its channel list (visibility), a busy chat (replies, files, embeds, reactions, system messages), a Q&A channel whose posts start threads, a thread from its first message (type 21), a forum's posts, a search.
PSEUDOLAB = "944032730050621450"
REQUESTS = [
    ("guild", f"guilds/{PSEUDOLAB}", {"with_counts": "true"}),
    ("my_member", f"users/@me/guilds/{PSEUDOLAB}/member", None),
    ("guild_channels", f"guilds/{PSEUDOLAB}/channels", None),
    ("chat_page", "channels/944039831317659648/messages", {"limit": "100", "before": "1567000000000000000"}),
    ("qna_page", "channels/944039762052923403/messages", {"limit": "30"}),
    ("thread_start", "channels/1554901528249114625/messages", {"limit": "5", "after": "1554901528249114625"}),
    ("forum_threads", "channels/1159641854514573432/threads/search", {"archived": "true", "sort_by": "last_message_time", "sort_order": "desc", "limit": "25", "offset": "0"}),
    ("search", f"guilds/{PSEUDOLAB}/messages/search", {"content": "스터디", "max_id": "1567000000000000000", "limit": "25", "offset": "0"}),
]


def main() -> int:
    token, _ = credentials.load()
    http = api.Http(token, token_hint=credentials.hint())
    OUT.mkdir(parents=True, exist_ok=True)
    for name, path, query in REQUESTS:
        if (OUT / f"{name}.json").exists():
            continue
        body = http.get(path, query)
        (OUT / f"{name}.json").write_text(json.dumps({"path": path, "query": query, "status": 200, "body": body}, ensure_ascii=False, indent=1))
        print(f"captured {name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
