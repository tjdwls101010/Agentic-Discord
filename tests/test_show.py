"""`show` through the real entry point: what the archive holds, printed with no request, and what the printout says about what it does not hold.

Every archive state here is made by real commands against the toy Discord (`read` for ranges, `search` for stray rows, a delete and a re-read for absence, a thread read for a thread's first entry), never by inserting rows. Expected lines come from what the test posted, read and deleted.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from support.fake_discord import TOKEN, FakeDiscord, ToyWorld
from support.harness import closed_port_url, run_cli, tokenless_copy


def local_minute(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


class ShowCase(unittest.TestCase):
    def setUp(self) -> None:
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()
        self.addCleanup(self.restore_tz)
        w = ToyWorld()
        self.world = w
        self.ana = w.user("ana", "Ana")
        self.g = w.guild("Lab")
        self.ch = w.channel("general", guild=self.g)
        self.quiet = w.channel("quiet", guild=self.g)
        self.ids = []
        for i in range(20):
            self.ids.append(w.post(self.ch, self.ana, f"m{i}" + (" needle" if i in (0, 7, 14) else "")))
            w.tick(60)
        self.far = [w.post(self.quiet, self.ana, f"far {i} alpha") for i in range(3)]
        w.tick(3600)
        self.fake = FakeDiscord(w).__enter__()
        self.addCleanup(self.fake.__exit__)
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data)
        self.cwd = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.cwd)
        self.env = {"DISCORD_TOKEN": TOKEN, "DISCORD_API_BASE": self.fake.url, "DISCORD_SKILL_DATA": str(self.data),
                    "DISCORD_REQUEST_GAP": "0"}

    def restore_tz(self) -> None:
        if self.old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()

    def cli(self, *args, **kw):
        r = run_cli(*args, env={**self.env, **kw.pop("env", {})}, cwd=str(self.cwd), **kw)
        return r

    def ok(self, *args, **kw):
        r = self.cli(*args, **kw)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def read_range(self, first: int, last: int) -> None:
        """Read ids[first..last] inclusive, as one read-through range."""
        self.ok("read", str(self.ch), "--after", str(self.ids[first - 1]), "--before", str(self.ids[last + 1]), "--quiet")

    def show(self, *ids: int, **kw):
        """`show` with no way to reach Discord: any request it tried would fail."""
        self.fake.requests.clear()
        r = self.cli("show", *map(str, ids), env={"DISCORD_API_BASE": closed_port_url()}, **kw)
        self.assertEqual(self.fake.requests, [])
        return r

    def body(self, *ids: int) -> list[str]:
        r = self.show(*ids)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.split("\n\n", 1)[1].splitlines()

    def gaps(self, *ids: int) -> list[str]:
        return [line for line in self.body(*ids) if line.startswith("⋯")]


class Gaps(ShowCase):
    def test_1_neighbours_inside_one_range_have_no_line_between(self) -> None:
        self.read_range(2, 5)
        self.assertEqual(self.gaps(self.ids[3], self.ids[4]), [])

    def test_2_a_message_left_out_inside_one_range_is_counted(self) -> None:
        self.read_range(2, 5)
        lines = self.body(self.ids[3], self.ids[5])
        self.assertEqual(lines[lines.index("⋯ 1 not shown") + 1][-len(f"[{self.ids[5]}]"):], f"[{self.ids[5]}]")
        self.assertEqual(self.gaps(self.ids[3], self.ids[5]), ["⋯ 1 not shown"])

    def test_3_between_two_ranges_is_not_read_through_with_stored_strays_counted(self) -> None:
        self.read_range(2, 5)
        self.read_range(10, 12)
        self.assertEqual(self.gaps(self.ids[5], self.ids[10]), ["⋯ not read through"])
        self.ok("search", "Lab", "--content", "needle")          # stores ids[0], ids[7], ids[14] as stray rows
        self.assertEqual(self.gaps(self.ids[5], self.ids[10]), ["⋯ 1 not shown; not read through"])
        self.assertEqual(self.gaps(self.ids[4], self.ids[11]), ["⋯ 3 not shown; not read through"])

    def test_4_search_hits_alone_are_not_read_through_between_each_and_counted_outside(self) -> None:
        self.ok("search", "Lab", "--content", "alpha")
        r = self.show(*self.far)
        head, body = r.stdout.split("\n\n", 1)
        self.assertEqual([x for x in body.splitlines() if x.startswith("⋯")], ["⋯ not read through"] * 2)
        self.assertIn("outside read-through ranges: 3", head)
        self.assertIn("read through: nowhere here", body)


class Absence(ShowCase):
    def test_5_a_message_a_reread_did_not_find_is_gone_and_one_that_came_back_is_not(self) -> None:
        self.read_range(2, 5)
        kept = dict(self.world.messages[self.ch][self.ids[4]])
        self.world.delete(self.ids[3])
        self.world.delete(self.ids[4])
        self.read_range(2, 5)
        self.world.messages[self.ch][self.ids[4]] = kept
        self.read_range(2, 5)
        lines = self.body(*self.ids[2:6])
        self.assertIn(f"m3  [{self.ids[3]}] (gone)", "\n".join(lines))
        self.assertIn(f"m4  [{self.ids[4]}]", lines[[i for i, x in enumerate(lines) if f"[{self.ids[4]}]" in x][0]])
        self.assertEqual(sum("(gone)" in x for x in lines), 1)


class ThreadStarts(ShowCase):
    def make_thread(self, original: int, *, deleted_original: bool = False) -> tuple[int, int]:
        """A thread made from `original`: its channel id is the original's, and its first entry is a stub of its own."""
        w = self.world
        t = w.channel("talk", guild=self.g, type=11, parent=self.ch)
        w.channels[t]["id"] = str(original)
        w.channels[original] = w.channels.pop(t)
        w.threads[self.ch] = [original if x == t else x for x in w.threads[self.ch]]
        stub = w.post(original, self.ana, "", type=21, message_reference={"message_id": str(original), "channel_id": str(self.ch)},
                      referenced_message=None if deleted_original else w.messages[self.ch][original])
        return original, stub

    def test_6_a_thread_start_shows_the_original_and_its_gone_mark_is_the_stub_s(self) -> None:
        thread, stub = self.make_thread(self.ids[8])
        self.ok("channels", "Lab")                                  # names #general, as a session would have
        self.ok("read", str(thread), "--quiet")
        out = "\n".join(self.body(stub))
        self.assertIn(f"(thread start; the original is [{self.ids[8]}] in #general", out)
        self.world.delete(stub)
        self.world.tick(1)
        self.world.post(thread, self.ana, "later reply")            # a newer message keeps the re-read's range above the stub
        self.ok("read", str(thread), "--quiet")
        out = "\n".join(self.body(stub, self.ids[8]))
        self.assertIn(f"m8  [{self.ids[8]}]\n", out + "\n")          # the original, in the parent channel, is not gone
        self.assertIn(f"[{stub}] (gone)", out)
        self.assertNotIn(f"[{self.ids[8]}] (gone)", out)

    def test_6_a_thread_start_whose_original_was_deleted(self) -> None:
        thread, stub = self.make_thread(self.ids[9], deleted_original=True)
        self.ok("read", str(thread), "--quiet")
        self.assertIn(f"* thread start: the original message was deleted  [{stub}]", "\n".join(self.body(stub)))


class ReadThrough(ShowCase):
    def test_7_the_line_names_the_range_read_and_only_that(self) -> None:
        self.read_range(2, 5)
        expect = f"read through: {local_minute(self.world.created[self.ids[1]])} → {local_minute(self.world.created[self.ids[6]])}"
        self.assertEqual(self.body(self.ids[3])[1], expect)
        self.ok("search", "Lab", "--content", "needle")            # ids[0] and ids[7] become stray rows around the range
        lines = self.body(self.ids[0], self.ids[3], self.ids[7])
        self.assertEqual(lines[1], expect)
        self.assertEqual([x for x in lines if x.startswith("⋯")], ["⋯ 1 not shown; not read through", "⋯ 2 not shown; not read through"])

    def test_7_a_channel_with_only_stray_rows_was_read_through_nowhere(self) -> None:
        self.ok("search", "Lab", "--content", "alpha")
        self.assertEqual(self.body(self.far[0])[1], "read through: nowhere here (these rows came from search or as reply targets)")

    def test_groups_are_per_channel_in_order_of_their_first_message(self) -> None:
        self.read_range(2, 5)
        self.ok("search", "Lab", "--content", "alpha")
        r = self.show(self.far[1], self.ids[3], self.far[0])
        head, body = r.stdout.split("\n\n", 1)
        groups = [x for x in body.splitlines() if x.startswith("══")]
        self.assertEqual(groups, [f"══ Lab › #general (text) [{self.ch}] · 1 messages ══", f"══ Lab › #quiet (text) [{self.quiet}] · 2 messages ══"])
        self.assertIn("show: 3 messages in 2 channels", head)


class SameAsRead(ShowCase):
    def test_8_show_prints_what_read_printed_but_for_the_group_lines(self) -> None:
        read = self.ok("read", str(self.ch), "--last", "20")
        read_body = read.split("\n\n", 1)[1]
        lines = self.body(*self.ids)
        self.assertEqual(lines[0], f"══ Lab › #general (text) [{self.ch}] · 20 messages ══")
        self.assertEqual("\n".join(lines[2:]), read_body.rstrip("\n"))


class HeaderAndRefusals(ShowCase):
    def test_the_header_says_no_request_when_rows_were_fetched_and_what_is_missing(self) -> None:
        self.read_range(2, 5)
        unknown = self.ids[-1] + 999
        r = self.show(self.ids[3], unknown)
        head = r.stdout.split("\n\n", 1)[0]
        self.assertIn("no request made", head)
        self.assertIn("fetched: ", head)
        self.assertIn("later edits, reactions and deletions may not be in it", head)
        self.assertIn(f"not in the archive: 1 ({unknown})", head)
        self.assertIn("result: ", head)
        result = Path(head.split("result: ")[1].strip())
        self.assertTrue(result.resolve().is_relative_to((self.cwd / ".discord" / "results").resolve()), result)

    def test_over_inline_only_the_header_is_printed(self) -> None:
        self.read_range(2, 5)
        r = self.show(*self.ids[2:6], "--inline", "2")
        self.assertIn("(not printed: 4 messages > --inline 2; read the result file)", r.stdout)
        self.assertNotIn("m3", r.stdout)

    def test_ids_come_from_stdin_and_links_too(self) -> None:
        self.read_range(2, 5)
        link = f"https://discord.com/channels/{self.g}/{self.ch}/{self.ids[4]}"
        r = self.cli("show", "-", link, stdin=f"{self.ids[2]}\n{self.ids[3]}\n", env={"DISCORD_API_BASE": closed_port_url()})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("show: 3 messages in 1 channels", r.stdout)

    def test_9_anything_but_an_id_on_stdin_refuses_the_whole_call(self) -> None:
        self.read_range(2, 5)
        r = self.cli("show", "-", stdin=f"{self.ids[2]}\n{self.ids[3]}|m3\nnull\n")
        self.assertEqual(r.returncode, 2, r.stdout)
        out = json.loads(r.stdout)
        self.assertEqual(out["error"], "usage")
        self.assertIn(f"'{self.ids[3]}|m3'", out["message"])
        self.assertIn("'null'", out["message"])

    def test_9_with_no_archive_it_refuses_and_creates_none(self) -> None:
        r = self.show(self.ids[0])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(json.loads(r.stdout)["error"], "refused")
        self.assertFalse((self.data / "discord.db").exists())

    def test_9_it_runs_with_no_token_and_no_reachable_discord(self) -> None:
        self.read_range(2, 5)
        copy = tokenless_copy()
        self.addCleanup(shutil.rmtree, copy.parent)
        env = {"DISCORD_API_BASE": closed_port_url(), "DISCORD_SKILL_DATA": str(self.data)}
        r = run_cli("show", str(self.ids[3]), env=env, cwd=str(self.cwd), scripts=copy)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("m3", r.stdout)

    def test_none_of_the_ids_in_the_archive_is_refused(self) -> None:
        self.read_range(2, 5)
        r = self.show(self.ids[15], self.ids[16])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("none of the 2", json.loads(r.stdout)["message"])


class TokenRefusalsUnchanged(unittest.TestCase):
    """The other commands still read the token first: with none they refuse as before and leave no archive behind; a rejected one is `auth`."""

    def test_no_token_refuses_before_the_archive_is_touched(self) -> None:
        copy = tokenless_copy()
        self.addCleanup(shutil.rmtree, copy.parent)
        data = Path(tempfile.mkdtemp())
        for args in (("read", "123456789012345678"), ("servers",), ("search", "x"), ("get", "users/@me")):
            with self.subTest(args=args):
                r = run_cli(*args, env={"DISCORD_API_BASE": closed_port_url(), "DISCORD_SKILL_DATA": str(data)}, scripts=copy)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertEqual(json.loads(r.stdout)["error"], "auth")
        self.assertFalse((data / "discord.db").exists())

    def test_a_rejected_token_is_auth(self) -> None:
        with FakeDiscord(ToyWorld()) as fake:
            data = Path(tempfile.mkdtemp())
            r = run_cli("servers", env={"DISCORD_TOKEN": "rotated", "DISCORD_API_BASE": fake.url, "DISCORD_SKILL_DATA": str(data)})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(json.loads(r.stdout)["error"], "auth")


if __name__ == "__main__":
    unittest.main()
