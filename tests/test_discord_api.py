"""The Discord unit's contract: GET only, the waits and retries Discord asks for, failures that never pass for empty results, times and ids read the way a user writes them, and channel access worked out from roles."""

from __future__ import annotations

import inspect
import os
import time
import unittest
from datetime import datetime, timedelta

from support.fake_discord import TOKEN, FakeDiscord
from support.harness import engine

api = engine("discord.discord_api")
errors = engine("discord.errors")

MSGS = r"/api/v9/channels/\d+/messages"


class HttpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeDiscord().__enter__()
        self.addCleanup(self.fake.__exit__)
        self.slept: list[float] = []
        self.http = api.Http(TOKEN, token_hint="write DISCORD_TOKEN=… in /x/.env", base_url=self.fake.url, gap=0,
                             sleep=self.slept.append)
        self.discord = api.Discord(self.http)
        w = self.fake.world
        self.author = w.user("ana")
        self.g = w.guild("g")
        self.ch = w.channel("general", guild=self.g)
        for i in range(3):
            w.post(self.ch, self.author, f"hello {i}")


class Requests(HttpCase):
    def test_every_request_is_a_get_with_a_user_agent_and_the_token(self) -> None:
        self.discord.messages(self.ch)
        self.discord.me()
        self.assertTrue(all(r["method"] == "GET" for r in self.fake.requests))
        self.assertTrue(all(r["headers"].get("User-Agent") == api.USER_AGENT for r in self.fake.requests))

    def test_the_http_layer_has_no_way_to_choose_a_method(self) -> None:
        self.assertNotIn("method", inspect.signature(api.Http.get).parameters)
        self.assertEqual([n for n in dir(api.Http) if n.lower() in ("post", "put", "patch", "delete", "request")], [])

    def test_requests_are_spaced_by_the_gap(self) -> None:
        http = api.Http(TOKEN, token_hint="x", base_url=self.fake.url, gap=0.2)
        started = time.monotonic()
        for _ in range(3):
            http.get("users/@me")
        self.assertGreaterEqual(time.monotonic() - started, 0.4)

    def test_after_and_before_are_never_sent_together(self) -> None:
        with self.assertRaises(ValueError):
            self.discord.messages(self.ch, after=1, before=2)

    def test_a_page_comes_back_oldest_first_with_the_ids_received(self) -> None:
        msgs, ids = self.discord.messages(self.ch)
        self.assertEqual([m.content for m in msgs], ["hello 0", "hello 1", "hello 2"])
        self.assertEqual(ids, {m.id for m in msgs})


class Retries(HttpCase):
    def test_429_waits_retry_after_then_succeeds(self) -> None:
        self.fake.script(MSGS, (429, {"message": "You are being rate limited.", "retry_after": 1.5, "global": False}))
        msgs, _ = self.discord.messages(self.ch)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(self.slept, [1.5])

    def test_429_after_five_waits_is_a_failure(self) -> None:
        self.fake.script(MSGS, *[(429, {"retry_after": 0.1})] * 6)
        with self.assertRaises(api.HttpFailure) as cm:
            self.discord.messages(self.ch)
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(len(self.slept), 5)

    def test_a_429_asking_for_minutes_stops_instead_of_waiting(self) -> None:
        self.fake.script(MSGS, (429, {"retry_after": 600}))
        with self.assertRaises(api.HttpFailure):
            self.discord.messages(self.ch)
        self.assertEqual(self.slept, [])

    def test_202_while_search_indexes_is_retried(self) -> None:
        path = rf"/api/v9/channels/{self.ch}/messages/search"
        self.fake.script(path, (202, {"message": "Index not yet available. Try again later", "code": 110000, "retry_after": 2}))
        total, hits = self.discord.search(channel_id=self.ch, query=[("content", "hello")])
        self.assertEqual(total, 3)
        self.assertEqual(self.slept, [2.0])

    def test_408_and_5xx_back_off_1_2_4_8_then_fail(self) -> None:
        self.fake.script(MSGS, (408, {}), (502, {}))
        self.assertEqual(len(self.discord.messages(self.ch)[0]), 3)
        self.assertEqual(self.slept, [1, 2])
        self.slept.clear()
        self.fake.script(MSGS, *[(503, {})] * 5)
        with self.assertRaises(api.HttpFailure) as cm:
            self.discord.messages(self.ch)
        self.assertEqual(self.slept, [1, 2, 4, 8])
        self.assertEqual(cm.exception.kind, "failed")

    def test_a_200_that_is_not_a_list_is_not_an_empty_page(self) -> None:
        self.fake.script(MSGS, (200, {"messages": []}))
        with self.assertRaises(api.HttpFailure):
            self.discord.messages(self.ch)
        self.fake.script(MSGS, (200, b"<html>maintenance</html>"))
        with self.assertRaises(api.HttpFailure):
            self.discord.messages(self.ch)


class Refusals(HttpCase):
    def test_401_says_where_to_write_the_token_and_never_shows_it(self) -> None:
        http = api.Http("wrong-token", token_hint="write DISCORD_TOKEN=… in /x/.env", base_url=self.fake.url, gap=0)
        with self.assertRaises(api.HttpFailure) as cm:
            http.get("users/@me")
        self.assertEqual(cm.exception.kind, "auth")
        self.assertIn("/x/.env", cm.exception.fix)
        self.assertNotIn("wrong-token", str(cm.exception) + cm.exception.fix)

    def test_403_50001_is_no_access(self) -> None:
        self.fake.world.forbidden.add(self.ch)
        with self.assertRaises(api.HttpFailure) as cm:
            self.discord.messages(self.ch)
        self.assertEqual((cm.exception.kind, cm.exception.code), ("forbidden", 50001))
        self.assertIn("no access", cm.exception.message)
        self.assertIsInstance(cm.exception, errors.Refusal)

    def test_a_member_who_left_is_an_answer_not_a_failure(self) -> None:
        self.assertEqual(self.discord.member(self.g, self.author), (None, None, False))


class Times(unittest.TestCase):
    NOW = datetime(2026, 11, 11, 12, 0).astimezone()

    def test_ids_links_relative_and_dates(self) -> None:
        self.assertEqual(api.parse_bound("123456789012345678"), 123456789012345678)
        self.assertEqual(api.parse_bound("https://discord.com/channels/1/2/123456789012345678"), 123456789012345678)
        with self.assertRaises(errors.Refusal):
            api.parse_bound("12345")
        week = api.parse_bound("7d", now=self.NOW)
        self.assertEqual(api.time_of(week), (self.NOW - timedelta(days=7)).replace(microsecond=0))
        self.assertEqual(api.time_of(api.parse_bound("30m", now=self.NOW)), self.NOW - timedelta(minutes=30))
        self.assertEqual(api.time_of(api.parse_bound("12h", now=self.NOW)), self.NOW - timedelta(hours=12))
        with self.assertRaises(errors.Refusal):
            api.parse_bound("last tuesday")

    def test_a_date_alone_is_local_midnight(self) -> None:
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()
        try:
            t = api.time_of(api.parse_bound("2026-11-01"))
            self.assertEqual(t.isoformat(), "2026-10-31T15:00:00+00:00")
            t = api.time_of(api.parse_bound("2026-11-01T09:30"))
            self.assertEqual(t.isoformat(), "2026-11-01T00:30:00+00:00")
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_snowflake_time_round_trip(self) -> None:
        # Discord's documented example: 175928847299117063 was created 2016-04-30 11:18:25.796 UTC.
        self.assertEqual(api.time_of(175928847299117063).isoformat(), "2016-04-30T11:18:25.796000+00:00")
        self.assertEqual(api.at(api.time_of(175928847299117063)) >> 22, 175928847299117063 >> 22)
        self.assertEqual(api.from_discord_time("2026-01-02T03:04:05.678000+00:00"), "2026-01-02T03:04:05.678000Z")


class Access(unittest.TestCase):
    G, ME, ROLE, OTHER = 1, 50, 7, 8

    def access(self, *, everyone=0, roles=None, mine=(), overwrites=(), owner=99):
        perms = {self.G: everyone, **(roles or {})}
        return api.channel_access(server_id=self.G, owner_id=owner, my_id=self.ME, my_roles=list(mine),
                                  role_permissions=perms, overwrites=list(overwrites))

    V, H, ADMIN = 1 << 10, 1 << 16, 1 << 3

    def test_cases(self) -> None:
        V, H = self.V, self.H
        self.assertEqual(self.access(everyone=V | H), (True, True))
        self.assertEqual(self.access(everyone=V), (True, False))
        self.assertEqual(self.access(owner=self.ME), (True, True))
        self.assertEqual(self.access(roles={self.ROLE: self.ADMIN}, mine=[self.ROLE]), (True, True))
        hidden = [{"id": str(self.G), "type": 0, "allow": "0", "deny": str(V)}]
        self.assertEqual(self.access(everyone=V | H, overwrites=hidden), (False, False))
        # a role's allow beats @everyone's deny
        self.assertEqual(self.access(everyone=V | H, mine=[self.ROLE], overwrites=hidden + [{"id": str(self.ROLE), "type": 0, "allow": str(V), "deny": "0"}]), (True, True))
        # among roles, allow wins over another role's deny
        both = [{"id": str(self.ROLE), "type": 0, "allow": "0", "deny": str(V)}, {"id": str(self.OTHER), "type": 0, "allow": str(V), "deny": "0"}]
        self.assertEqual(self.access(everyone=V | H, mine=[self.ROLE, self.OTHER], overwrites=both), (True, True))
        # the member's own overwrite comes last
        self.assertEqual(self.access(everyone=V | H, overwrites=[{"id": str(self.ME), "type": 1, "allow": "0", "deny": str(V)}]), (False, False))
        # a role's overwrite does not apply to someone without the role
        self.assertEqual(self.access(everyone=V | H, overwrites=hidden + [{"id": str(self.ROLE), "type": 0, "allow": str(V), "deny": "0"}]), (False, False))


if __name__ == "__main__":
    unittest.main()
