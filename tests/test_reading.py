"""The collector against the toy Discord: which messages each range takes, where the 100-message pages meet, what an interrupted read leaves, and --new. Then `read`, `search` and `get` through the real entry point."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from support.fake_discord import TOKEN, FakeDiscord, ToyWorld, snowflake
from support.harness import engine, run_cli

api = engine("discord.discord_api")
archive = engine("discord.archive")
reading = engine("discord.reading")
errors = engine("discord.errors")


class CollectorCase(unittest.TestCase):
    N = 250

    def setUp(self) -> None:
        self.world = ToyWorld()
        w = self.world
        self.author = w.user("ana")
        self.ch = w.channel("general", guild=w.guild("g"))
        self.ids = []
        for i in range(self.N):
            self.ids.append(w.post(self.ch, self.author, f"m{i}"))
            w.tick(60)
        w.tick(3600)
        self.fake = FakeDiscord(w).__enter__()
        self.addCleanup(self.fake.__exit__)
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.db = archive.Archive(self.dir / "discord.db")
        self.addCleanup(self.db.close)
        self.api = api.Discord(api.Http(TOKEN, token_hint="x", base_url=self.fake.url, gap=0))
        self.db.save_channels([self.api.channel(self.ch)], "2026-01-01T00:00:00.000000Z")
        self.fake.requests.clear()

    def collect(self, spec):
        return reading.collect(self.api, self.db, self.ch, spec, now=lambda: self.world.now)

    def got(self, out) -> list[int]:
        return [m.id for m in out.messages]

    def cov(self):
        return [(r.lo, r.hi) for r in self.db.coverage(self.ch)]


class Directions(CollectorCase):
    def test_after_excludes_its_bound_and_walks_oldest_first(self) -> None:
        out = self.collect(reading.Spec("after", 1000, after=self.ids[10]))
        self.assertEqual(self.got(out), self.ids[11:])
        self.assertTrue(out.reached_head)
        self.assertEqual(self.cov(), [(self.ids[10] + 1, self.ids[-1])])

    def test_after_and_before_exclude_both_bounds_and_never_send_both(self) -> None:
        out = self.collect(reading.Spec("after", 1000, after=self.ids[10], before=self.ids[150]))
        self.assertEqual(self.got(out), self.ids[11:150])
        sent = [r["query"] for r in self.fake.calls(r"/api/v9/channels/\d+/messages")]
        self.assertTrue(all(not ("after" in q and "before" in q) for q in sent), sent)
        self.assertEqual(self.cov(), [(self.ids[10] + 1, self.ids[150] - 1)])

    def test_before_alone_walks_newest_first(self) -> None:
        out = self.collect(reading.Spec("before", 1000, before=self.ids[120]))
        self.assertEqual(self.got(out), self.ids[:120])
        self.assertTrue(out.reached_start)
        self.assertEqual(self.cov(), [(self.ch, self.ids[120] - 1)])

    def test_last_takes_the_newest_n_and_says_older_remain(self) -> None:
        out = self.collect(reading.Spec("last", 30))
        self.assertEqual(self.got(out), self.ids[-30:])
        self.assertEqual((out.cut, out.resume), ("older", {"before": self.ids[-30]}))
        self.assertEqual(self.db.head(self.ch).hi, self.ids[-1])

    def test_around_is_one_window(self) -> None:
        out = self.collect(reading.Spec("around", 10, around=self.ids[100]))
        self.assertEqual(self.got(out), self.ids[95:105])
        self.assertEqual(len(self.fake.requests), 1)
        self.assertIsNone(self.db.head(self.ch))


class PageBoundaries(CollectorCase):
    def test_exactly_100_then_the_next_page_continues_without_gap_or_overlap(self) -> None:
        out = self.collect(reading.Spec("after", 200, after=self.ids[0]))
        self.assertEqual(self.got(out), self.ids[1:201])
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual((out.cut, out.resume), ("newer", {"after": self.ids[200]}))
        self.assertEqual(self.cov(), [(self.ids[0] + 1, self.ids[200])])
        rest = self.collect(reading.Spec("after", 200, **out.resume))
        self.assertEqual(self.got(rest), self.ids[201:])
        self.assertEqual(self.cov(), [(self.ids[0] + 1, self.ids[-1])])

    def test_a_limit_inside_a_page_asks_discord_for_only_that_many(self) -> None:
        self.collect(reading.Spec("last", 130))
        self.assertEqual([r["query"]["limit"] for r in self.fake.requests], [["100"], ["30"]])

    def test_a_channel_of_exactly_one_page(self) -> None:
        w = self.world
        ch = w.channel("small", guild=int(w.channels[self.ch]["guild_id"]))
        ids = [w.post(ch, self.author, "x") for _ in range(100)]
        self.db.save_channels([self.api.channel(ch)], "t")
        out = reading.collect(self.api, self.db, ch, reading.Spec("last", 500), now=lambda: w.now)
        self.assertEqual([m.id for m in out.messages], ids)
        self.assertTrue(out.reached_start)
        self.assertEqual([(r.lo, r.hi) for r in self.db.coverage(ch)], [(ch, ids[-1])])


class Scenarios(CollectorCase):
    def test_a_newest_first_read_cut_by_the_limit_claims_only_what_it_stored(self) -> None:
        """Scenario 1, through the collector."""
        self.collect(reading.Spec("before", 150, before=self.ids[200]))
        self.assertEqual(self.cov(), [(self.ids[50], self.ids[200] - 1)])

    def test_an_empty_answer_between_two_ranges_closes_the_gap(self) -> None:
        """Scenario 6, through the collector: the only message between two read ranges is deleted."""
        self.collect(reading.Spec("after", 10, after=self.ids[0]))           # ids[1..10]
        self.collect(reading.Spec("after", 10, after=self.ids[11]))          # ids[12..21]
        self.world.delete(self.ids[11])
        out = self.collect(reading.Spec("after", 100, after=self.ids[10], before=self.ids[12]))
        self.assertEqual(self.got(out), [])
        self.assertEqual(self.cov(), [(self.ids[0] + 1, self.ids[21])])

    def test_a_failure_mid_walk_is_raised_and_stores_nothing_for_that_page(self) -> None:
        """Scenario 9, through the collector: a 200 that is not a list is not an empty page."""
        self.fake.script(r"/api/v9/channels/\d+/messages", (200, {"oops": True}))
        with self.assertRaises(api.HttpFailure):
            self.collect(reading.Spec("last", 50))
        self.assertEqual(self.cov(), [])
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_a_thread_read_stores_the_stub_in_the_thread_and_the_original_in_the_parent(self) -> None:
        """Scenario 11, from the API's JSON: the thread's id is the original's; the stub has its own id."""
        w = self.world
        original = self.ids[100]
        thread = w.channel("t", guild=int(w.channels[self.ch]["guild_id"]), type=11, parent=self.ch)
        w.channels[thread]["id"] = str(original)
        w.channels[original] = w.channels.pop(thread)
        stub = w.post(original, self.author, "", type=21, message_reference={"message_id": str(original), "channel_id": str(self.ch)},
                      referenced_message=w.messages[self.ch][original])
        self.db.save_channels([self.api.channel(original)], "t")
        out = reading.collect(self.api, self.db, original, reading.Spec("last", 10), now=lambda: w.now)
        self.assertEqual([m.kind for m in out.messages], ["thread_starter"])
        rows = dict(self.db.db.execute("SELECT id, channel_id FROM messages WHERE id IN (?, ?)", (original, stub)))
        self.assertEqual(rows, {original: self.ch, stub: original})


class Interrupted(CollectorCase):
    def test_an_interrupted_read_keeps_its_committed_pages_and_skip_covered_continues(self) -> None:
        """Scenario 7: the third page fails and keeps failing; the first two are stored and true, and the rerun fetches only the rest."""
        original = self.world.answer
        calls = {"n": 0}

        def third_page_down(path, q):
            if path.endswith("/messages"):
                calls["n"] += 1
                if calls["n"] >= 3:
                    return 503, {"message": "down"}
            return original(path, q)
        self.world.answer = third_page_down
        self.api.http._sleep = lambda s: None
        with self.assertRaises(api.HttpFailure):
            self.collect(reading.Spec("after", 1000, after=self.ids[0]))
        self.assertEqual(self.cov(), [(self.ids[0] + 1, self.ids[200])])
        rest = self.collect(reading.Spec("after", 200, **out.resume))
        self.assertEqual(self.got(rest), self.ids[201:])
        self.assertEqual(self.cov(), [(self.ids[0] + 1, self.ids[-1])])

    def test_a_limit_inside_a_page_asks_discord_for_only_that_many(self) -> None:
        self.collect(reading.Spec("last", 130))
        self.assertEqual([r["query"]["limit"] for r in self.fake.requests], [["100"], ["30"]])

    def test_a_channel_of_exactly_one_page(self) -> None:
        w = self.world
        ch = w.channel("small", guild=int(w.channels[self.ch]["guild_id"]))
        ids = [w.post(ch, self.author, "x") for _ in range(100)]
        self.db.save_channels([self.api.channel(ch)], "t")
        out = reading.collect(self.api, self.db, ch, reading.Spec("last", 500), now=lambda: w.now)
        self.assertEqual([m.id for m in out.messages], ids)
        self.assertTrue(out.reached_start)
        self.assertEqual([(r.lo, r.hi) for r in self.db.coverage(ch)], [(ch, ids[-1])])


class Scenarios(CollectorCase):
    def test_a_newest_first_read_cut_by_the_limit_claims_only_what_it_stored(self) -> None:
        """Scenario 1, through the collector."""
        self.collect(reading.Spec("before", 150, before=self.ids[200]))
        self.assertEqual(self.cov(), [(self.ids[50], self.ids[200] - 1)])

    def test_an_empty_answer_between_two_ranges_closes_the_gap(self) -> None:
        """Scenario 6, through the collector: the only message between two read ranges is deleted."""
        self.collect(reading.Spec("after", 10, after=self.ids[0]))           # ids[1..10]
        self.collect(reading.Spec("after", 10, after=self.ids[11]))          # ids[12..21]
        self.world.delete(self.ids[11])
        out = self.collect(reading.Spec("after", 100, after=self.ids[10], before=self.ids[12]))
        self.assertEqual(self.got(out), [])
        self.assertEqual(self.cov(), [(self.ids[0] + 1, self.ids[21])])

    def test_a_failure_mid_walk_is_raised_and_stores_nothing_for_that_page(self) -> None:
        """Scenario 9, through the collector: a 200 that is not a list is not an empty page."""
        self.fake.script(r"/api/v9/channels/\d+/messages", (200, {"oops": True}))
        with self.assertRaises(api.HttpFailure):
            self.collect(reading.Spec("last", 50))
        self.assertEqual(self.cov(), [])
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_a_thread_read_stores_the_stub_in_the_thread_and_the_original_in_the_parent(self) -> None:
        """Scenario 11, from the API's JSON: the thread's id is the original's; the stub has its own id."""
        w = self.world
        original = self.ids[100]
        thread = w.channel("t", guild=int(w.channels[self.ch]["guild_id"]), type=11, parent=self.ch)
        w.channels[thread]["id"] = str(original)
        w.channels[original] = w.channels.pop(thread)
        stub = w.post(original, self.author, "", type=21, message_reference={"message_id": str(original), "channel_id": str(self.ch)},
                      referenced_message=w.messages[self.ch][original])
        self.db.save_channels([self.api.channel(original)], "t")
        out = reading.collect(self.api, self.db, original, reading.Spec("last", 10), now=lambda: w.now)
        self.assertEqual([m.kind for m in out.messages], ["thread_starter"])
        rows = dict(self.db.db.execute("SELECT id, channel_id FROM messages WHERE id IN (?, ?)", (original, stub)))
        self.assertEqual(rows, {original: self.ch, stub: original})


class Interrupted(CollectorCase):
    def test_an_interrupted_read_keeps_its_committed_pages_and_skip_covered_continues(self) -> None:
        """Scenario 7: the third page fails; the first two are stored and true, and the rerun fetches only the rest."""
        self.fake.script(r"/api/v9/channels/\d+/messages", *[], )
        calls = {"n": 0}
        original = self.fake.world.answer

        def flaky(path, q):
            if path.endswith("/messages"):
                calls["n"] += 1
                if calls["n"] == 3:
                    return 500, {"message": "boom"}
            return original(path, q)
        self.fake.world.answer = flaky
        self.api.http._sleep = lambda s: None
        from support.fake_discord import FakeDiscord as _  # noqa: F401  (the server answers through world.answer)
        self.fake.script(r"/api/v9/channels/\d+/messages")
        with self.assertRaises(api.HttpFailure):
            calls["n"] = 0
            self.world.answer = flaky
            # every retry of the third request fails too
            def always_fail_third(path, q):
                if path.endswith("/messages"):
                    calls["n"] += 1
                    if calls["n"] >= 3:
                        return 503, {"message": "down"}
                return original(path, q)
            self.world.answer = always_fail_third
            self.collect(reading.Spec("after", 1000, after=self.ids[0]))
        self.assertEqual(self.cov(), [(self.ids[0] + 1, self.ids[200])])
        self.world.answer = original
        self.fake.requests.clear()
        out = self.collect(reading.Spec("after", 1000, after=self.ids[0], skip_covered=True))
        self.assertEqual(self.got(out), self.ids[1:])
        self.assertEqual((out.fetched, out.from_archive), (self.N - 201, 200))
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.cov(), [(self.ids[0] + 1, self.ids[-1])])

    def test_skip_covered_newest_first_resumes_below_the_covered_suffix(self) -> None:
        self.collect(reading.Spec("last", 100))
        self.fake.requests.clear()
        out = self.collect(reading.Spec("before", 1000, before=self.ids[-1] + 1, skip_covered=True))
        self.assertEqual(self.got(out), self.ids)
        self.assertEqual(out.from_archive, 100)
        self.assertEqual([r["query"].get("before") for r in self.fake.requests], [[str(self.ids[-100])], [str(self.ids[-200])]])

    def test_without_skip_covered_a_range_is_fetched_again(self) -> None:
        self.collect(reading.Spec("last", 50))
        self.fake.requests.clear()
        self.collect(reading.Spec("last", 50))
        self.assertEqual(len(self.fake.requests), 1)


class New(CollectorCase):
    def test_new_is_refused_until_the_archive_has_reached_the_newest_message(self) -> None:
        with self.assertRaises(errors.Refusal) as cm:
            self.collect(reading.Spec("new", 200))
        self.assertIn("--last", cm.exception.fix)
        self.collect(reading.Spec("around", 10, around=self.ids[-3]))
        with self.assertRaises(errors.Refusal):
            self.collect(reading.Spec("new", 200))

    def test_new_takes_what_was_posted_since_and_only_that(self) -> None:
        """Scenario 2: the head's upper bound is the newest message received, so later posts are above it."""
        self.collect(reading.Spec("last", 5))
        w = self.world
        later = []
        for _ in range(130):
            w.tick(1)
            later.append(w.post(self.ch, self.author, "later"))
        first = self.collect(reading.Spec("new", 100))
        self.assertEqual(first.cut, "newer")
        second = self.collect(reading.Spec("new", 100))
        self.assertTrue(second.reached_head)
        self.assertEqual(self.got(first) + self.got(second), later)
        self.assertEqual(self.db.head(self.ch).hi, later[-1])

    def test_new_makes_progress_when_the_overlap_alone_fills_the_limit(self) -> None:
        """Ten stored messages inside the 5-second overlap and --limit 3: the overlap must not use up the limit."""
        w = self.world
        burst = []
        for _ in range(10):
            w.tick(0.2)
            burst.append(w.post(self.ch, self.author, "burst"))
        self.collect(reading.Spec("last", 20))
        w.tick(1)
        fresh = w.post(self.ch, self.author, "fresh")
        out = self.collect(reading.Spec("new", 3))
        self.assertEqual(self.got(out), [fresh])
        self.assertTrue(out.reached_head)

    def test_new_shows_everything_since_the_last_walk_to_the_newest_even_if_another_read_stored_some(self) -> None:
        """Scenario 4, and a window that touches the head: posts since the head stay new to --new."""
        self.collect(reading.Spec("last", 5))
        w = self.world
        later = []
        for _ in range(30):
            w.tick(1)
            later.append(w.post(self.ch, self.author, "later"))
        self.collect(reading.Spec("around", 10, around=later[-5]))       # a window far above the head
        self.collect(reading.Spec("around", 10, around=later[3]))        # one touching the head
        out = self.collect(reading.Spec("new", 1000))
        self.assertEqual(self.got(out), later)

    def test_search_hits_do_not_move_where_new_starts(self) -> None:
        """Scenario 8."""
        self.collect(reading.Spec("last", 5))
        w = self.world
        newer = [w.post(self.ch, self.author, "word") for _ in range(3)]
        _, hits = self.api.search(server_id=int(w.channels[self.ch]["guild_id"]), query=[("content", "word")])
        self.db.add_strays(hits, "2026-12-01T00:00:00.000000Z")
        self.assertEqual(self.db.head(self.ch).hi, self.ids[-1])
        out = self.collect(reading.Spec("new", 100))
        self.assertEqual(self.got(out), newer)


class CommandsThroughTheEntryPoint(unittest.TestCase):
    """`read`, `search` and `get` as the model runs them: header first, the result file, refusals as one JSON line."""

    def setUp(self) -> None:
        w = ToyWorld()
        self.world = w
        self.ana = w.user("ana", "Ana")
        self.bob = w.user("bob")
        self.g = w.guild("Lab")
        self.ch = w.channel("general", guild=self.g)
        self.forum = w.channel("ideas", guild=self.g, type=15)
        self.hidden = w.channel("staff", guild=self.g, overwrites=[{"id": str(self.g), "type": 0, "allow": "0", "deny": str(1 << 10)}])
        w.members[(self.g, self.ana)] = {"user": w.users[self.ana], "nick": "Ana-nick", "roles": []}
        self.first = w.post(self.ch, self.ana, "hello study group")
        w.tick(60)
        self.reply = w.post(self.ch, self.bob, "hi <@%d>" % self.ana, type=19, message_reference={"message_id": str(self.first)},
                            referenced_message=w.messages[self.ch][self.first])
        self.fake = FakeDiscord(w).__enter__()
        self.addCleanup(self.fake.__exit__)
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data)
        self.env = {"DISCORD_TOKEN": TOKEN, "DISCORD_API_BASE": self.fake.url, "DISCORD_SKILL_DATA": str(self.data), "DISCORD_REQUEST_GAP": "0"}

    def cli(self, *args):
        return run_cli(*args, env=self.env)

    def test_read_prints_a_header_then_the_transcript_and_keeps_it_in_a_new_file(self) -> None:
        r = self.cli("read", str(self.ch), "--last", "10")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        head, _, body = r.stdout.partition("\n\n")
        self.assertTrue(head.startswith(f"read: Lab › #general  (text) [{self.ch}]"), head)
        self.assertIn("got: 2 messages", head)
        self.assertIn("end: reached the channel's first message; also its newest", head)
        result = Path(head.split("result: ")[1].strip())
        self.assertTrue(result.is_file())
        self.assertIn("Ana-nick: hello study group", body)
        self.assertIn('bob (reply to Ana-nick: "hello study group"): hi @Ana-nick', body)
        self.assertEqual(result.read_text().strip().split("\n\n", 1)[1], body.strip())
        r2 = self.cli("read", str(self.ch), "--last", "1")
        self.assertNotEqual(r2.stdout.split("result: ")[1].split("\n")[0], str(result))
        self.assertTrue(result.is_file())

    def test_read_prints_only_the_header_over_inline_and_with_quiet(self) -> None:
        r = self.cli("read", str(self.ch), "--inline", "1")
        self.assertIn("(not printed: 2 messages > --inline 1; read the result file)", r.stdout)
        self.assertNotIn("hello study group", r.stdout)
        self.fake.requests.clear()
        r = self.cli("read", str(self.ch), "--quiet")
        self.assertNotIn("hello", r.stdout)
        self.assertEqual([x["path"] for x in self.fake.requests if "/members/" in x["path"]], [])

    def test_a_name_resolves_after_channels_lists_it(self) -> None:
        r = self.cli("read", "general")
        self.assertEqual(r.returncode, 1)
        self.assertIn("channels <server>", json.loads(r.stdout)["fix"])
        self.assertEqual(self.cli("channels", "Lab").returncode, 0)
        self.assertEqual(self.cli("read", "general", "--last", "1").returncode, 0)
        self.assertEqual(self.cli("read", "Lab/general", "--last", "1").returncode, 0)

    def test_channels_lists_what_you_can_see_and_counts_the_rest(self) -> None:
        r = self.cli("channels", str(self.g))
        self.assertIn("2 channels you can see, 1 hidden from you (not listed)", r.stdout)
        self.assertNotIn("staff", r.stdout)
        self.assertIn("#ideas", r.stdout)

    def test_a_forum_and_a_hidden_channel_are_refused_with_what_to_do(self) -> None:
        self.cli("channels", str(self.g))
        r = self.cli("read", str(self.forum))
        out = json.loads(r.stdout)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"threads {self.forum}", out["fix"])
        r = self.cli("read", str(self.hidden))
        self.assertEqual(json.loads(r.stdout)["error"], "forbidden")

    def test_conflicting_ranges_are_a_usage_error(self) -> None:
        r = self.cli("read", str(self.ch), "--last", "5", "--after", "7d")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout)["error"], "usage")
        r = self.cli("read", str(self.ch), "--after", "yesterday-ish")
        self.assertEqual(r.returncode, 2)

    def test_search_finds_whole_words_names_the_channel_and_pages(self) -> None:
        r = self.cli("search", "Lab", "--content", "study")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("total: ~1", r.stdout)
        self.assertIn("#general · Ana-nick: hello study group", r.stdout)
        r = self.cli("search", "Lab", "--content", "stud", "--count")
        self.assertIn("total: ~0", r.stdout)

    def test_search_from_a_name_seen_before_and_refuses_an_unknown_one(self) -> None:
        r = self.cli("search", "Lab", "--from", "bob")
        self.assertEqual(json.loads(r.stdout)["error"], "refused")
        self.cli("read", str(self.ch))
        r = self.cli("search", "Lab", "--from", "bob")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("bob (reply to", r.stdout)
        self.assertEqual(self.fake.calls(r"/api/v9/guilds/\d+/messages/search")[-1]["query"]["author_id"], [str(self.bob)])

    def test_get_reaches_any_path_and_renders_messages_on_request(self) -> None:
        r = self.cli("get", f"channels/{self.ch}/messages", "--query", "limit=5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('"content": "hello study group"', r.stdout)
        r = self.cli("get", f"channels/{self.ch}/messages", "--transcript")
        self.assertIn("messages: 2 found in the response", r.stdout)
        self.assertIn("hello study group", r.stdout)
        self.assertTrue(all(x["method"] == "GET" for x in self.fake.requests))


if __name__ == "__main__":
    unittest.main()
