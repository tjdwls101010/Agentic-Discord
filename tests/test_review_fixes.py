"""Cases from the pre-merge review, through the real entry point: ambiguous search targets, threads across both lists, malformed 200s, the token in what Discord sends back, and the --inline bound of `get`."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from support.fake_discord import TOKEN, FakeDiscord, ToyWorld
from support.harness import run_cli


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.world = ToyWorld()
        self.fake = FakeDiscord(self.world).__enter__()
        self.addCleanup(self.fake.__exit__)
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data)
        self.env = {"DISCORD_TOKEN": TOKEN, "DISCORD_API_BASE": self.fake.url, "DISCORD_SKILL_DATA": str(self.data), "DISCORD_REQUEST_GAP": "0"}

    def cli(self, *args):
        return run_cli(*args, env=self.env)


class AmbiguousTargets(Case):
    def test_a_name_shared_by_servers_and_a_dm_is_refused_with_all_candidates(self) -> None:
        w = self.world
        a, b = w.guild("same"), w.guild("same")
        friend = w.user("friend", "same")
        dm = w.channel(None, type=1, recipients=[friend])
        self.cli("servers")
        r = self.cli("search", "same", "--content", "x")
        self.assertEqual(r.returncode, 1, r.stdout)
        out = json.loads(r.stdout)
        self.assertIn("matches", out["message"])
        for i in (a, b, dm):
            self.assertIn(str(i), out["message"])

    def test_one_server_and_one_dm_with_the_same_name_is_still_ambiguous(self) -> None:
        w = self.world
        w.guild("same")
        w.channel(None, type=1, recipients=[w.user("friend", "same")])
        self.cli("servers")
        self.assertEqual(self.cli("search", "same", "--content", "x").returncode, 1)


class ThreadsAcrossBothLists(Case):
    def test_a_newer_archived_thread_is_not_crowded_out_by_an_older_active_one(self) -> None:
        w = self.world
        u = w.user("ana")
        ch = w.channel("general", guild=w.guild("g"))
        active = w.channel("old but active", guild=None, type=11, parent=ch)
        w.post(active, u, "x")
        w.tick(3600)
        archived = w.channel("newer, archived", guild=None, type=11, parent=ch, archived=True)
        w.post(archived, u, "y")
        out = self.cli("threads", str(ch), "--limit", "1").stdout
        self.assertIn(str(archived), out)
        self.assertNotIn(str(active), out)


class MalformedAnswers(Case):
    def test_a_search_200_without_messages_is_a_failure_not_no_hits(self) -> None:
        g = self.world.guild("g")
        self.fake.script(rf"/api/v9/guilds/{g}/messages/search", (200, {"error": "upstream failure"}))
        r = self.cli("search", str(g), "--content", "x")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("no more hits", r.stdout)

    def test_a_thread_listing_200_without_threads_is_a_failure(self) -> None:
        ch = self.world.channel("general", guild=self.world.guild("g"))
        self.fake.script(rf"/api/v9/channels/{ch}/threads/search", (200, {"error": "upstream failure"}))
        self.assertEqual(self.cli("threads", str(ch)).returncode, 1)


class TokenStaysHidden(Case):
    def test_a_token_reflected_in_an_error_message_is_not_printed(self) -> None:
        self.fake.script(r"/api/v9/users/@me", (401, {"message": f"bad token {TOKEN}", "code": 0}), (401, {"message": f"bad token {TOKEN}", "code": 0}))
        for args in (("doctor",), ("get", "users/@me")):
            r = self.cli(*args)
            self.assertNotIn(TOKEN, r.stdout + r.stderr)

    def test_a_redirect_is_not_followed_with_the_token(self) -> None:
        with FakeDiscord(ToyWorld()) as elsewhere:
            self.fake.script(r"/api/v9/users/@me", (302, b"", {"Location": elsewhere.url + "/users/@me"}))
            r = self.cli("get", "users/@me")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertEqual(elsewhere.requests, [])


class GetInline(Case):
    def test_json_over_inline_thousand_characters_is_not_printed(self) -> None:
        ch = self.world.channel("general", guild=self.world.guild("g"))
        u = self.world.user("ana")
        self.world.post(ch, u, "x" * 1200)
        r = self.cli("get", f"channels/{ch}/messages", "--inline", "1")
        self.assertIn("not printed", r.stdout)
        self.assertNotIn("x" * 100, r.stdout)


if __name__ == "__main__":
    unittest.main()
