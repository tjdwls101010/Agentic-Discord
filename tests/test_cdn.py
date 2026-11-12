"""The Discord unit's CDN contract: a link is fetched only when it is an https link to Discord's own CDN hosts, with no token and no redirect followed; a complete answer is told apart from a cut one, an expired link from a failure; and attachments are parsed with their id, size in pixels and expiry.

Links use Discord's real host names; `DISCORD_CDN_BASE` points them at the local fake only after the link itself was checked, so a broken check shows up as a request the fake recorded, never as one to the network. Hosts that must be refused are under `.test` and `.invalid`.
"""

from __future__ import annotations

import io
import os
import time
import unittest

from support.fake_cdn import GONE, NO_LENGTH, SHORT, FakeCdn
from support.harness import engine

api = engine("discord.discord_api")
errors = engine("discord.errors")

PNG = bytes(range(256)) * 40          # 10,240 bytes the test wrote itself


class CdnCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cdn = FakeCdn().__enter__()
        self.addCleanup(self.cdn.__exit__)
        self.old = os.environ.get("DISCORD_CDN_BASE")
        os.environ["DISCORD_CDN_BASE"] = self.cdn.base
        self.addCleanup(self.restore)
        self.path = "/attachments/500/900/chart.png"
        self.cdn.add(self.path, PNG, "image/png")

    def restore(self) -> None:
        if self.old is None:
            os.environ.pop("DISCORD_CDN_BASE", None)
        else:
            os.environ["DISCORD_CDN_BASE"] = self.old

    def fetch(self, url: str, max_bytes: int = 10 ** 9):
        out = io.BytesIO()
        return api.download(url, out, max_bytes=max_bytes), out.getvalue()


class Fetching(CdnCase):
    def test_10_a_live_link_brings_the_bytes_with_no_token_and_only_a_get(self) -> None:
        d, data = self.fetch(self.cdn.link(self.path))
        self.assertEqual((d.outcome, d.content_type, d.size), ("saved", "image/png", len(PNG)))
        self.assertEqual(data, PNG)
        (req,) = self.cdn.requests
        self.assertEqual(req["method"], "GET")
        self.assertNotIn("authorization", {k.lower() for k in req["headers"]})
        self.assertEqual(req["headers"].get("User-Agent"), api.USER_AGENT)

    def test_the_media_proxy_host_is_also_discord_s(self) -> None:
        d, _ = self.fetch(self.cdn.link(self.path).replace("cdn.discordapp.com", "media.discordapp.net"))
        self.assertEqual(d.outcome, "saved")

    def test_an_expired_or_re_signed_link_is_expired_not_a_failure(self) -> None:
        for url in (self.cdn.link(self.path, life=-60), self.cdn.link(self.path).replace("hm=", "hm=0")):
            with self.subTest(url=url):
                d, data = self.fetch(url)
                self.assertEqual(d.outcome, "expired")
                self.assertNotIn(GONE, data)

    def test_17_an_answer_cut_short_of_its_length_is_a_failure(self) -> None:
        self.cdn.script(self.path, SHORT)
        d, _ = self.fetch(self.cdn.link(self.path))
        self.assertEqual(d.outcome, "failed")

    def test_an_answer_without_a_length_is_a_failure(self) -> None:
        self.cdn.script(self.path, NO_LENGTH)
        d, _ = self.fetch(self.cdn.link(self.path))
        self.assertEqual(d.outcome, "failed")
        self.assertIn("Content-Length", d.detail)

    def test_a_file_over_the_limit_is_not_taken(self) -> None:
        d, data = self.fetch(self.cdn.link(self.path), max_bytes=len(PNG) - 1)
        self.assertEqual(d.outcome, "too_large")
        self.assertEqual(data, b"")
        d, data = self.fetch(self.cdn.link(self.path), max_bytes=len(PNG))
        self.assertEqual(d.outcome, "saved")

    def test_another_status_is_a_failure_that_names_it(self) -> None:
        self.cdn.script(self.path, (500, b"oops", {}))
        d, _ = self.fetch(self.cdn.link(self.path))
        self.assertEqual(d.outcome, "failed")
        self.assertIn("500", d.detail)


class Refusing(CdnCase):
    def test_20_only_https_links_to_discord_s_cdn_hosts_are_requested(self) -> None:
        good = self.cdn.link(self.path)
        rest = good.split("cdn.discordapp.com", 1)[1]
        bad = [
            "https://evil.test" + rest,
            "https://cdn.discordapp.com.evil.test" + rest,
            "https://evilcdn.discordapp.com" + rest,
            "http://cdn.discordapp.com" + rest,
            "https://user:pw@cdn.discordapp.com" + rest,
            "https://cdn.discordapp.com@evil.test" + rest,
            "https://cdn.discordapp.com:8443" + rest,
            "https://cdn.discordapp.com:443" + rest,
            "https://cdn.discordapp.com." + rest,
            "ftp://cdn.discordapp.com" + rest,
            "https://cdn%2Ediscordapp.com" + rest,
            "https://cdn.discordapp.com\\@evil.test" + rest,
            "https://evil.test\\@cdn.discordapp.com" + rest,
            "https://cdn.discordapp.com%40evil.test" + rest,
            "",
        ]
        for url in bad:
            with self.subTest(url=url):
                d, _ = self.fetch(url)
                self.assertEqual(d.outcome, "refused")
        self.assertEqual(self.cdn.requests, [])

    def test_20_a_redirect_is_a_failure_and_is_not_followed(self) -> None:
        with FakeCdn() as elsewhere:
            elsewhere.add(self.path, b"other")
            self.cdn.script(self.path, (302, b"", {"Location": elsewhere.base + self.path}))
            d, data = self.fetch(self.cdn.link(self.path))
            self.assertEqual(d.outcome, "failed")
            self.assertEqual(data, b"")
            self.assertEqual(elsewhere.requests, [])

    def test_the_test_base_must_be_loopback_http(self) -> None:
        for base in ("http://evil.test:8080", "https://127.0.0.1:1", "http://127.0.0.1", "http://127.0.0.1:1/x", "http://10.0.0.1:80"):
            with self.subTest(base=base):
                os.environ["DISCORD_CDN_BASE"] = base
                with self.assertRaises(errors.Refusal):
                    self.fetch(self.cdn.link(self.path))
        os.environ["DISCORD_CDN_BASE"] = self.cdn.base.replace("127.0.0.1", "localhost")
        self.assertEqual(self.fetch(self.cdn.link(self.path))[0].outcome, "saved")
        self.assertTrue(all(r["method"] == "GET" for r in self.cdn.requests))


class Parsing(unittest.TestCase):
    def test_an_attachment_keeps_its_id_size_in_pixels_and_expiry(self) -> None:
        ex = 0x6AF677F0
        raw = {"id": "9", "channel_id": "500", "type": 0, "content": "", "author": {"id": "1", "username": "a"},
               "timestamp": "2026-11-11T00:00:00+00:00", "attachments": [
                   {"id": "948030439203930132", "filename": "chart.png", "size": 24040, "width": 839, "height": 236, "content_type": "image/png",
                    "url": f"https://cdn.discordapp.com/attachments/500/948030439203930132/chart.png?ex={ex:x}&is=6af52670&hm=1e41&",
                    "proxy_url": "https://media.discordapp.net/attachments/500/948030439203930132/chart.png?ex=1&is=2&hm=3&"},
                   {"id": "948030439203930133", "filename": "notes.pdf", "size": 10, "url": "https://cdn.discordapp.com/attachments/500/948030439203930133/notes.pdf"}]}
        a, b = api.parse_message(raw).attachments
        self.assertEqual((a.id, a.width, a.height, a.size, a.content_type), (948030439203930132, 839, 236, 24040, "image/png"))
        self.assertEqual(a.expires_at, time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(ex)))
        self.assertEqual((b.id, b.width, b.height, b.expires_at), (948030439203930133, None, None, None))


if __name__ == "__main__":
    unittest.main()
