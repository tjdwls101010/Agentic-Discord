"""`files` through the real entry point, against the toy Discord and the fake CDN: which requests a download costs, what lands on disk and where, and what is said about what did not.

Every message is posted in the toy world and read into the archive with `read`, as a session would. A link expired in the archive is made by reading while the world hands out expired links (`link_life` below zero); the next answer from the world carries live ones again, as Discord's does. Expected bytes are the ones the test wrote; expected names are worked out by hand from the rule in the plan.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from support.fake_cdn import SHORT, FakeCdn
from support.fake_discord import TOKEN, FakeDiscord, ToyWorld
from support.harness import closed_port_url, run_cli, tokenless_copy

PNG = bytes(range(256)) * 40          # 10,240 bytes
JPG = b"\xff\xd8" + bytes(5000)       # 5,002 bytes
MSGS = r"/api/v9/channels/\d+/messages"


class FilesCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cdn = FakeCdn().__enter__()
        self.addCleanup(self.cdn.__exit__)
        w = ToyWorld()
        w.cdn = self.cdn
        self.world = w
        self.ana = w.user("ana", "Ana")
        self.g = w.guild("Lab")
        self.ch = w.channel("general", guild=self.g)
        self.fake = FakeDiscord(w).__enter__()
        self.addCleanup(self.fake.__exit__)
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data)
        self.cwd = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.cwd)
        self.env = {"DISCORD_TOKEN": TOKEN, "DISCORD_API_BASE": self.fake.url, "DISCORD_SKILL_DATA": str(self.data),
                    "DISCORD_REQUEST_GAP": "0", "DISCORD_CDN_BASE": self.cdn.base}

    # -- making the world and the archive --------------------------------------------------------------

    def post(self, *files, channel: int | None = None, content: str = "look") -> tuple[int, list[dict]]:
        """A message carrying `files` (each (filename, bytes) or (filename, bytes, options for ToyWorld.attachment))."""
        ch = channel or self.ch
        atts = [self.world.attachment(ch, f[0], f[1], **(f[2] if len(f) > 2 else {})) for f in files]
        mid = self.world.post(ch, self.ana, content, attachments=atts)
        self.world.tick(60)
        return mid, atts

    def read(self, channel: int | None = None, *, expired: bool = False) -> None:
        self.world.link_life = -3600 if expired else 86400
        r = run_cli("read", str(channel or self.ch), "--last", "100", "--quiet", env=self.env, cwd=str(self.cwd))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.world.link_life = 86400

    # -- running files ------------------------------------------------------------------------------------

    def files(self, *args, env: dict | None = None, scripts: Path | None = None, cwd: Path | None = None):
        self.fake.requests.clear()
        self.cdn.requests.clear()
        return run_cli("files", *map(str, args), env={**self.env, **(env or {})}, cwd=str(cwd or self.cwd), scripts=scripts)

    def ok(self, *args, **kw) -> str:
        r = self.files(*args, **kw)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def dir(self, mid: int, cwd: Path | None = None) -> Path:
        return (cwd or self.cwd) / ".discord" / "files" / str(mid)

    def names(self, mid: int) -> set[str]:
        d = self.dir(mid)
        return {p.name for p in d.iterdir()} if d.exists() else set()

    def api_calls(self) -> list[dict]:
        return self.fake.calls(MSGS)

    def first_line(self, out: str) -> str:
        return out.splitlines()[0]

    def all_get(self) -> None:
        self.assertTrue(all(r["method"] == "GET" for r in self.fake.requests + self.cdn.requests))


class LiveLinks(FilesCase):
    def test_10_live_links_cost_no_api_request_and_send_no_token(self) -> None:
        mid, (a, b) = self.post(("chart.png", PNG, {"width": 2096, "height": 1698}), ("photo.jpg", JPG, {"content_type": "image/jpeg"}))
        self.read()
        out = self.ok(mid)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(len(self.cdn.requests), 2)
        self.assertTrue(all("authorization" not in {k.lower() for k in r["headers"]} for r in self.cdn.requests))
        self.assertEqual((self.dir(mid) / f"{a['id']}-chart.png").read_bytes(), PNG)
        self.assertEqual((self.dir(mid) / f"{b['id']}-photo.jpg").read_bytes(), JPG)
        self.assertIn(f"  {self.dir(mid) / (a['id'] + '-chart.png')}  image/png 10 KB 2096×1698", out)
        self.assertIn(f"  {self.dir(mid) / (b['id'] + '-photo.jpg')}  image/jpeg 5 KB", out)
        self.assertIn(f"[{mid}] ", out)
        self.assertIn("Ana · #general", out)
        self.assertIn("downloaded 2", self.first_line(out))
        self.all_get()

    def test_10_live_links_need_no_token_at_all(self) -> None:
        mid, (a,) = self.post(("chart.png", PNG))
        self.read()
        copy = tokenless_copy()
        self.addCleanup(shutil.rmtree, copy.parent)
        env = {k: v for k, v in self.env.items() if k != "DISCORD_TOKEN"}
        env["DISCORD_API_BASE"] = closed_port_url()
        r = run_cli("files", str(mid), env=env, cwd=str(self.cwd), scripts=copy)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.dir(mid) / f"{a['id']}-chart.png").read_bytes(), PNG)

    def test_16_a_second_run_asks_nothing_even_once_the_links_expired(self) -> None:
        mid, _ = self.post(("chart.png", PNG), ("photo.jpg", JPG))
        self.read()
        self.ok(mid)
        self.read(expired=True)
        out = self.ok(mid)
        self.assertEqual((self.fake.requests, self.cdn.requests), ([], []))
        self.assertIn("already here 2", self.first_line(out))

    def test_17_a_file_that_differs_from_its_declaration_is_kept_as_it_arrived(self) -> None:
        mid, (a,) = self.post(("chart.png", PNG, {"declared_type": "image/webp", "declared_size": 999}))
        self.read()
        out = self.ok(mid)
        self.assertEqual((self.dir(mid) / f"{a['id']}-chart.png").read_bytes(), PNG)
        self.assertIn("image/png 10 KB", out)
        self.assertNotIn("webp", out)

    def test_17_an_answer_cut_short_leaves_no_file_and_no_partial_file(self) -> None:
        mid, (a,) = self.post(("chart.png", PNG))
        self.read()
        self.cdn.script(urlparse(a["url"]).path, SHORT)
        out = self.ok(mid)
        self.assertEqual(self.names(mid), set())
        self.assertIn("not downloaded:", out)
        self.assertIn("chart.png", out.split("not downloaded:")[1])

    def test_15_over_max_mb_is_not_taken_by_declaration_or_by_what_arrives(self) -> None:
        big = bytes(2 * 1024 * 1024)
        declared, (d,) = self.post(("huge.zip", PNG, {"declared_size": 50 * 1024 * 1024, "content_type": "application/zip"}))
        lying, (l,) = self.post(("small.bin", big, {"declared_size": 10, "content_type": "application/octet-stream"}))
        self.read()
        out = self.ok(declared, lying, "--max-mb", "1")
        self.assertEqual(self.names(declared) | self.names(lying), set())
        reasons = out.split("not downloaded:")[1]
        self.assertIn("huge.zip", reasons)
        self.assertIn("small.bin", reasons)
        self.assertEqual([urlparse(r["path"]).path for r in self.cdn.requests], [urlparse(l["url"]).path])   # the declared one was never asked for


class Refreshing(FilesCase):
    def test_11_an_expired_link_costs_one_request_per_message_and_the_new_link_is_used(self) -> None:
        mid, (a, b) = self.post(("chart.png", PNG), ("photo.jpg", JPG))
        self.read(expired=True)
        out = self.ok(mid)
        calls = self.api_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["query"].get("around"), calls[0]["query"].get("limit")), ([str(mid)], ["3"]))
        self.assertEqual((self.dir(mid) / f"{a['id']}-chart.png").read_bytes(), PNG)
        self.assertEqual((self.dir(mid) / f"{b['id']}-photo.jpg").read_bytes(), JPG)
        self.assertIn("fetched again 1 messages in 1 API requests", self.first_line(out))
        self.all_get()

    def test_12_a_message_missing_from_the_refreshed_answer_takes_nothing_from_its_neighbours(self) -> None:
        before, _ = self.post(("before.png", PNG))
        target, _ = self.post(("target.png", PNG))
        after, _ = self.post(("after.png", PNG))
        self.read(expired=True)
        self.world.delete(target)
        out = self.ok(target)
        self.assertEqual(len(self.api_calls()), 1)
        self.assertIn("not in the refreshed answer", out)
        self.assertEqual(self.names(target) | self.names(before) | self.names(after), set())
        self.assertEqual(self.cdn.requests, [])

    def test_12_an_empty_refreshed_answer_says_the_same(self) -> None:
        lone = self.world.channel("lone", guild=self.g)
        target, _ = self.post(("target.png", PNG), channel=lone)
        self.read(lone, expired=True)
        self.world.delete(target)
        out = self.ok(target)
        self.assertIn("not in the refreshed answer", out)
        self.assertEqual(self.names(target), set())

    def test_13_a_message_that_no_longer_has_the_attachment_says_so(self) -> None:
        mid, (a,) = self.post(("chart.png", PNG))
        self.read(expired=True)
        self.world.messages[self.ch][mid]["attachments"] = []
        out = self.ok(mid)
        self.assertIn("no longer has this attachment", out)
        self.assertEqual(self.names(mid), set())

    def test_14_a_rate_limit_then_success_brings_the_file_and_counts_every_request(self) -> None:
        mid, (a,) = self.post(("chart.png", PNG))
        self.read(expired=True)
        self.fake.script(MSGS, (429, {"message": "You are being rate limited.", "retry_after": 0, "global": False}))
        out = self.ok(mid)
        self.assertEqual((self.dir(mid) / f"{a['id']}-chart.png").read_bytes(), PNG)
        n = len(self.api_calls())
        self.assertEqual(n, 2)
        self.assertIn(f"fetched again 1 messages in {n} API requests", self.first_line(out))

    def test_15_past_refresh_the_rest_are_listed_with_the_read_that_refreshes_them(self) -> None:
        first, _ = self.post(("a.png", PNG))
        second, _ = self.post(("b.png", PNG))
        third, _ = self.post(("c.png", PNG))
        self.read(expired=True)
        out = self.ok(first, second, third, "--refresh", "1")
        self.assertEqual(len(self.api_calls()), 1)
        self.assertEqual(len(self.names(first)), 1)
        self.assertEqual(self.names(second) | self.names(third), set())
        self.assertIn("link expired; not refreshed", out)
        self.assertIn(f"read {self.ch} --after {second - 1} --before {third + 1}", out)

    def test_21_a_thread_start_takes_the_original_s_files_from_the_parent_channel(self) -> None:
        w = self.world
        original, (a,) = self.post(("chart.png", PNG))
        t = w.channel("talk", guild=self.g, type=11, parent=self.ch)
        w.channels[t]["id"] = str(original)
        w.channels[original] = w.channels.pop(t)
        w.threads[self.ch] = [original if x == t else x for x in w.threads[self.ch]]
        stub = w.post(original, self.ana, "", type=21, message_reference={"message_id": str(original), "channel_id": str(self.ch)},
                      referenced_message=w.messages[self.ch][original])
        self.read(original, expired=True)
        out = self.ok(stub)
        self.assertEqual((self.dir(original) / f"{a['id']}-chart.png").read_bytes(), PNG)
        self.assertFalse(self.dir(stub).exists())
        (call,) = self.api_calls()
        self.assertEqual((call["path"], call["query"].get("around")), (f"/api/v9/channels/{self.ch}/messages", [str(original)]))
        self.assertIn(f"[{original}] ", out)

    def test_22_a_rejected_token_stops_refreshing_but_live_links_still_download(self) -> None:
        first, _ = self.post(("a.png", PNG))
        second, _ = self.post(("b.png", PNG))
        self.read(expired=True)
        other = self.world.channel("other", guild=self.g)
        live, (c,) = self.post(("c.png", PNG), channel=other)
        self.read(other)
        r = self.files(first, second, live, env={"DISCORD_TOKEN": "rotated"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.api_calls()), 1)
        self.assertEqual((self.dir(live) / f"{c['id']}-c.png").read_bytes(), PNG)
        self.assertEqual(self.names(first) | self.names(second), set())
        tail = r.stdout.split("not downloaded:")[1]
        self.assertIn("refreshing stopped", tail)
        self.assertIn("DISCORD_TOKEN", tail)
        self.all_get()

    def test_22_no_token_stops_refreshing_with_the_fix(self) -> None:
        mid, _ = self.post(("a.png", PNG))
        self.read(expired=True)
        copy = tokenless_copy()
        self.addCleanup(shutil.rmtree, copy.parent)
        env = {k: v for k, v in self.env.items() if k != "DISCORD_TOKEN"}
        r = run_cli("files", str(mid), env=env, cwd=str(self.cwd), scripts=copy)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("refreshing stopped", r.stdout)
        self.assertIn(str(copy / ".env"), r.stdout)

    def test_22_access_refused_is_that_message_s_reason_and_the_next_goes_on(self) -> None:
        hidden = self.world.channel("hidden", guild=self.g)
        first, _ = self.post(("a.png", PNG), channel=hidden)
        second, (b,) = self.post(("b.png", PNG))
        self.read(hidden, expired=True)
        self.read(expired=True)
        self.world.forbidden.add(hidden)
        out = self.ok(first, second)
        self.assertEqual(len(self.api_calls()), 2)
        self.assertIn("access refused", out)
        self.assertEqual((self.dir(second) / f"{b['id']}-b.png").read_bytes(), PNG)


class Paths(FilesCase):
    def test_18_names_from_elsewhere_become_safe_names_directly_under_the_message(self) -> None:
        raw_names = ["../x", "/etc/x", "a\x07b\n.png", "", ".hidden", "가" * 300 + ".png"]
        mid, atts = self.post(*[(n, PNG) for n in raw_names])
        odd, _ = self.post(("odd.png", PNG, {"att_id": "12ab"}))
        self.read()
        out = self.ok(mid, odd)
        ids = [a["id"] for a in atts]
        got = self.names(mid)
        self.assertTrue({f"{ids[0]}-x", f"{ids[1]}-etcx", f"{ids[2]}-ab.png", f"{ids[3]}-file", f"{ids[4]}-hidden"} <= got, got)
        long = next(n for n in got if n.startswith(f"{ids[5]}-"))
        self.assertTrue(long.endswith(".png") and len(long.encode()) <= 255, long)
        self.assertEqual(len(got), 6)
        self.assertTrue(all((self.dir(mid) / n).is_file() for n in got))
        files_root = self.cwd / ".discord" / "files"
        self.assertTrue(all(len(p.relative_to(files_root).parts) == 2 for p in files_root.rglob("*")
                            if p.is_file() or len(p.relative_to(files_root).parts) > 1), sorted(files_root.rglob("*")))
        self.assertEqual([p for p in self.cwd.rglob("*") if p.is_file() and p.relative_to(self.cwd).parts[0] != ".discord"], [])
        self.assertFalse(self.dir(odd).exists())
        self.assertIn("odd.png", out.split("not downloaded:")[1])

    def test_19_a_symbolic_link_anywhere_on_the_way_is_refused_and_nothing_lands_where_it_points(self) -> None:
        mid, (a,) = self.post(("chart.png", PNG))
        self.read()
        name = f"{a['id']}-chart.png"
        for case in ("files", "message", "target"):
            with self.subTest(case=case):
                cwd = Path(tempfile.mkdtemp()).resolve()
                outside = Path(tempfile.mkdtemp()).resolve()
                (cwd / ".discord").mkdir()
                if case == "files":
                    (cwd / ".discord" / "files").symlink_to(outside, target_is_directory=True)
                elif case == "message":
                    (cwd / ".discord" / "files").mkdir()
                    self.dir(mid, cwd).symlink_to(outside, target_is_directory=True)
                else:
                    self.dir(mid, cwd).mkdir(parents=True)
                    (self.dir(mid, cwd) / name).symlink_to(outside / "planted")
                out = self.ok(mid, cwd=cwd)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertIn("chart.png", out.split("not downloaded:")[1])
                self.assertEqual(self.cdn.requests, [])

    def test_20_a_link_that_is_not_discord_s_cdn_is_never_requested(self) -> None:
        mid = self.world.post(self.ch, self.ana, "x", attachments=[{
            "id": str(self.world.next_id()), "filename": "x.png", "size": 10, "content_type": "image/png",
            "url": "https://cdn.discordapp.com.evil.test/attachments/1/2/x.png?ex=7fffffff&is=1&hm=2&"}])
        self.read()
        out = self.ok(mid)
        self.assertEqual(self.cdn.requests, [])
        self.assertIn("x.png", out.split("not downloaded:")[1])

    def test_23_in_a_fresh_repository_git_ignores_what_was_downloaded(self) -> None:
        mid, (a,) = self.post(("chart.png", PNG))
        self.read()
        repo = Path(tempfile.mkdtemp()).resolve()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        self.ok(mid, cwd=repo)
        rel = f".discord/files/{mid}/{a['id']}-chart.png"
        self.assertTrue((repo / rel).is_file())
        self.assertEqual(subprocess.run(["git", "check-ignore", "-q", rel], cwd=repo).returncode, 0)


class WhatWasAsked(FilesCase):
    def test_24_none_of_the_ids_in_the_archive_is_refused(self) -> None:
        mid, _ = self.post(("chart.png", PNG))
        self.read()
        r = self.files(mid + 1000, mid + 2000)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(json.loads(r.stdout)["error"], "refused")

    def test_24_some_missing_are_listed_and_the_rest_downloaded(self) -> None:
        mid, (a,) = self.post(("chart.png", PNG))
        plain = self.world.post(self.ch, self.ana, "no files here")
        self.read()
        out = self.ok(mid, mid + 1000, plain)
        self.assertTrue((self.dir(mid) / f"{a['id']}-chart.png").is_file())
        tail = out.split("not downloaded:")[1]
        self.assertIn(f"[{mid + 1000}] not in the archive", tail)
        self.assertIn(f"[{plain}] no attachments", tail)

    def test_no_archive_is_refused_and_none_is_made(self) -> None:
        r = self.files(123456789012345678)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertFalse((self.data / "discord.db").exists())


if __name__ == "__main__":
    unittest.main()
