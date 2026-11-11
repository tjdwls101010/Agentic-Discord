"""Real response shapes, anonymised (tests/support/scrub.py), replayed through the real entry point.

Expectations come from fixtures/expected.json, which the scrubber computed from the raw captures before anonymising them; the counts of visible and hidden channels were measured on the real account the day of the capture.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from support.fake_discord import TOKEN, FakeDiscord
from support.harness import FIXTURES, SKILL_DIR, run_cli

CAPTURES = SKILL_DIR / "data" / "captures"
#: Keys whose values are closed sets (a MIME type, an embed kind) and may legitimately survive scrubbing.
CLOSED = {"content_type", "type", "locale", "preferred_locale", "sort_by", "sort_order", "archived", "with_counts",
          "limit", "offset", "permissions", "allow", "deny", "flags", "permissions_new"}


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


EXPECTED = json.loads((FIXTURES / "expected.json").read_text())


@unittest.skipUnless(CAPTURES.is_dir() and any(CAPTURES.glob("*.json")), "no raw captures on this machine")
class NoOriginalValueSurvives(unittest.TestCase):
    def test_no_id_or_text_from_the_captures_is_in_the_fixtures(self) -> None:
        values: list[str] = []

        def strings(x):
            if isinstance(x, dict):
                for v in x.values():
                    strings(v)
            elif isinstance(x, list):
                for v in x:
                    strings(v)
            elif isinstance(x, str):
                values.append(x)
        for p in FIXTURES.glob("*.json"):
            strings(json.loads(p.read_text()))
        fixtures = "\n".join(values)   # values only: a key such as "title" is not a leak
        ids: set[str] = set()
        texts: set[str] = set()
        closed: set[str] = set()

        def walk(x, key=None):
            if isinstance(x, dict):
                for k, v in x.items():
                    walk(v, k)
            elif isinstance(x, list):
                for v in x:
                    walk(v, key)
            elif isinstance(x, str) and key in CLOSED:
                closed.add(x)
            elif isinstance(x, str):
                ids.update(re.findall(r"(?<!\d)\d{15,20}(?!\d)", x))
                if len(x) >= 4 and not re.fullmatch(r"[\d\-:T.+Z]+", x):
                    texts.add(x)
        for p in CAPTURES.glob("*.json"):
            walk(json.loads(p.read_text()))
        self.assertGreater(len(ids), 100)
        self.assertEqual(sorted(i for i in ids if i in fixtures), [])
        self.assertEqual(sorted(t for t in texts - closed if t in fixtures), [])


class Replays(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeDiscord().__enter__()
        self.addCleanup(self.fake.__exit__)
        member = load("my_member")["body"]
        self.fake.world.me = member["user"]
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data)
        self.env = {"DISCORD_TOKEN": TOKEN, "DISCORD_API_BASE": self.fake.url, "DISCORD_SKILL_DATA": str(self.data), "DISCORD_REQUEST_GAP": "0"}
        self.guild = load("guild")
        self.channels = load("guild_channels")["body"]
        self.gid = self.guild["body"]["id"]

    def serve(self, name: str, times: int = 1) -> dict:
        f = load(name)
        self.fake.script(re.escape("/api/v9/" + f["path"]), *[(200, f["body"])] * times)
        return f

    def serve_server(self, times: int = 1) -> None:
        for name in ("guild", "my_member", "guild_channels"):
            self.serve(name, times)

    def serve_channel(self, channel_id: str, times: int = 1) -> None:
        c = next(c for c in self.channels if c["id"] == channel_id)
        self.fake.script(re.escape(f"/api/v9/channels/{channel_id}"), *[(200, dict(c, guild_id=self.gid))] * times)

    def cli(self, *args):
        r = run_cli(*args, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def transcript(self, out: str) -> str:
        path = re.search(r"result: (.+)", out).group(1).strip()
        return Path(path).read_text().split("\n\n", 1)[1]

    def test_channels_counts_what_you_can_see_like_the_real_account(self) -> None:
        self.serve_server()
        out = self.cli("channels", self.gid)
        want = EXPECTED["guild_channels"]
        self.assertIn(f"{want['visible']} channels you can see, {want['hidden']} hidden from you", out)

    def check_page(self, name: str) -> None:
        f = load(name)
        channel = f["path"].split("/")[1]
        self.serve_server()
        self.cli("channels", self.gid)
        self.serve(name)
        out = self.cli("read", channel, "--last", "100", "--inline", "0")
        text = self.transcript(out)
        want = EXPECTED[name]
        ids = [int(m) for m in re.findall(r"  \[(\d+)\]$", text, re.M)]
        self.assertEqual(ids, want["ids_oldest_first"])
        self.assertEqual(text.count("(reply to a deleted message)"), want["replies_target_deleted"])
        self.assertEqual(len(re.findall(r"\(reply to (?!a deleted)[^\[]", text)), want["replies_with_target"])
        self.assertEqual(text.count("(thread start;"), want["thread_starters"])
        self.assertEqual(text.count(" started a thread: "), want["threads_created"])
        self.assertGreaterEqual(text.count("  + file: "), want["with_files"])

    def test_a_busy_chat_page(self) -> None:
        self.check_page("chat_page")

    def test_a_qna_page_whose_posts_start_threads(self) -> None:
        self.check_page("qna_page")

    def test_a_thread_read_from_its_first_message(self) -> None:
        f = load("thread_start")
        thread = f["path"].split("/")[1]
        parent = next(c["id"] for c in self.channels if c["name"] and c["type"] == 0)
        self.fake.script(re.escape(f"/api/v9/channels/{thread}"),
                         (200, {"id": thread, "type": 11, "name": "a thread", "guild_id": self.gid, "parent_id": parent}))
        self.serve("thread_start")
        out = self.cli("read", thread, "--last", "5", "--inline", "0")
        text = self.transcript(out)
        self.assertEqual(text.count("(thread start;"), EXPECTED["thread_start"]["thread_starters"])
        self.assertEqual([int(m) for m in re.findall(r"  \[(\d+)\]$", text, re.M)], EXPECTED["thread_start"]["ids_oldest_first"])

    def test_a_forums_posts(self) -> None:
        f = load("forum_threads")
        forum = f["path"].split("/")[1]
        self.serve_channel(forum)
        path = re.escape("/api/v9/" + f["path"])
        self.fake.script(path, (200, {"threads": [], "members": [], "has_more": False, "total_results": 0, "first_messages": []}), (200, f["body"]))
        out = self.cli("threads", forum)
        listed = [int(m) for m in re.findall(r"^  .*  \[(\d+)\]  ·", out, re.M)]
        self.assertEqual(sorted(listed), EXPECTED["forum_threads"]["ids"])
        self.assertIn(f"archived: {EXPECTED['forum_threads']['total']}", out)

    def test_a_server_search(self) -> None:
        self.serve("search")
        self.serve_server()
        out = self.cli("search", self.gid, "--content", "anything", "--inline", "0")
        self.assertIn(f"total: ~{EXPECTED['search']['total']}", out)
        text = self.transcript(out)
        self.assertEqual([int(m) for m in re.findall(r"  \[(\d+)\]$", text, re.M)], EXPECTED["search"]["ids"])


if __name__ == "__main__":
    unittest.main()
