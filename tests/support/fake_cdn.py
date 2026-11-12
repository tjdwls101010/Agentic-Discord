"""A local stand-in for Discord's CDN (cdn.discordapp.com), as measured with no token: a signed link answers 200 with the file while its `ex` (hex Unix seconds) is in the future and its `hm` matches; an expired, re-signed or unsigned link answers 404 with `This content is no longer available.`; no User-Agent answers 403.

The skill reaches it through `DISCORD_CDN_BASE`, which swaps only the scheme and host of a link, so links here are written with Discord's real host. `script(path, …)` queues odd answers for a path: a body cut short of its Content-Length, no Content-Length, a redirect.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "https://cdn.discordapp.com"
GONE = b"This content is no longer available."
#: Answers a test can queue for a path.
SHORT, NO_LENGTH = "short", "no_length"


def _sign(path: str, ex: int) -> str:
    return hashlib.sha256(f"{path}|{ex}".encode()).hexdigest()


class FakeCdn:
    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {}
        self.requests: list[dict] = []
        self.scripts: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                u = urlparse(self.path)
                with fake.lock:
                    fake.requests.append({"method": self.command, "path": u.path, "query": parse_qs(u.query), "headers": dict(self.headers)})
                    queued = fake.scripts[u.path].popleft() if fake.scripts[u.path] else None
                status, body, headers = fake._answer(self.command, u.path, parse_qs(u.query), dict(self.headers), queued)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if queued == SHORT:
                    self.send_header("Content-Length", str(len(body) * 2))
                elif queued != NO_LENGTH:
                    self.send_header("Content-Length", str(len(body)))
                else:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                if queued in (SHORT, NO_LENGTH):
                    self.close_connection = True

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def base(self) -> str:
        """What `DISCORD_CDN_BASE` is set to."""
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "FakeCdn":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def add(self, path: str, data: bytes, content_type: str = "image/png") -> None:
        self.files[path] = (data, content_type)

    def link(self, path: str, *, life: float = 86400) -> str:
        """A signed link to `path` that expires `life` seconds from now (negative: already expired)."""
        ex = int(time.time() + life)
        return f"{HOST}{path}?ex={ex:x}&is={int(time.time()):x}&hm={_sign(path, ex)}&"

    def script(self, path: str, *answers) -> None:
        """Queue answers for `path`: SHORT, NO_LENGTH, or (status, body, headers)."""
        self.scripts[path].extend(answers)

    def _answer(self, method: str, path: str, q, headers, queued):
        if isinstance(queued, tuple):
            return queued[0], queued[1], queued[2] if len(queued) > 2 else {}
        if method != "GET":
            return 405, b"", {}
        if not headers.get("User-Agent"):
            return 403, b"forbidden", {}
        try:
            ex = int(q["ex"][0], 16)
            good = secrets.compare_digest(q["hm"][0], _sign(path, ex)) and ex > time.time()
        except (KeyError, ValueError):
            good = False
        if not good or path not in self.files:
            return 404, GONE, {"Content-Type": "text/plain"}
        data, kind = self.files[path]
        return 200, data, {"Content-Type": kind}
