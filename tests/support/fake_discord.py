"""A local stand-in for Discord's REST API: an HTTP server on 127.0.0.1 that answers like Discord does for a user token.

Two layers. `FakeDiscord` is the server: it records every request and answers from `script`ed responses first, then from `world`. `ToyWorld` is a small Discord (servers, channels, users, messages) that pages, searches and lists threads with the behaviour measured on the real API: listings at most 100, newest first; `after` wins when `after` and `before` are both sent; search is whole-word and 25 at a time; channels you cannot see are listed anyway and answer 403 code 50001.
"""

from __future__ import annotations

import json
import re
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

EPOCH_MS = 1420070400000
TOKEN = "fake-token-for-tests.abcdef.not-a-real-discord-token-0000"
VIEW = 1 << 10
HISTORY = 1 << 16


def snowflake(moment: datetime, seq: int = 0) -> int:
    return ((int(moment.timestamp() * 1000) - EPOCH_MS) << 22) | (seq & 0xFFF)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class ToyWorld:
    """Servers, channels and messages with a clock that only moves when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.seq = 0
        self.me = {"id": "100000000000000001", "username": "me", "global_name": "Me"}
        self.users: dict[int, dict] = {int(self.me["id"]): self.me}
        self.guilds: dict[int, dict] = {}
        self.channels: dict[int, dict] = {}
        self.messages: dict[int, dict[int, dict]] = defaultdict(dict)   # channel -> id -> raw
        self.deleted: dict[int, datetime] = {}                           # id -> when
        self.created: dict[int, datetime] = {}
        self.members: dict[tuple[int, int], dict] = {}
        self.forbidden: set[int] = set()                                  # channels answering 403 50001
        self.threads: dict[int, list[int]] = defaultdict(list)            # parent -> thread ids

    def tick(self, seconds: float = 1.0) -> None:
        self.now += timedelta(seconds=seconds)

    def next_id(self) -> int:
        self.seq += 1
        self.tick(0.001)
        return snowflake(self.now, self.seq)

    def user(self, username: str, global_name: str | None = None, bot: bool = False) -> int:
        uid = self.next_id()
        u = {"id": str(uid), "username": username, "global_name": global_name}
        if bot:
            u["bot"] = True
        self.users[uid] = u
        return uid

    def guild(self, name: str, *, everyone_perms: int = VIEW | HISTORY, roles: dict | None = None, my_roles=(), owner: int | None = None) -> int:
        gid = self.next_id()
        rl = [{"id": str(gid), "name": "@everyone", "permissions": str(everyone_perms)}]
        for rid, (rname, perms) in (roles or {}).items():
            rl.append({"id": str(rid), "name": rname, "permissions": str(perms)})
        self.guilds[gid] = {"id": str(gid), "name": name, "owner_id": str(owner or 1), "roles": rl,
                            "my_roles": [str(r) for r in my_roles], "approximate_member_count": 10}
        return gid

    def channel(self, name: str | None, *, guild: int | None = None, type: int = 0, parent: int | None = None,
                overwrites=(), recipients=(), archived: bool = False) -> int:
        cid = self.next_id()
        c = {"id": str(cid), "type": type, "name": name, "position": len(self.channels),
             "permission_overwrites": list(overwrites)}
        if guild:
            c["guild_id"] = str(guild)
        if parent:
            c["parent_id"] = str(parent)
        if recipients:
            c["recipients"] = [self.users[r] for r in recipients]
        if type in (10, 11, 12):
            c["thread_metadata"] = {"archived": archived}
            c["message_count"] = 0
            self.threads[parent].append(cid)
        self.channels[cid] = c
        return cid

    def post(self, channel: int, author: int, content: str = "", **extra) -> int:
        mid = extra.pop("id", None) or self.next_id()
        raw = {"id": str(mid), "channel_id": str(channel), "type": 0, "content": content,
               "author": self.users[author], "timestamp": iso(self.now), "edited_timestamp": None,
               "attachments": [], "embeds": [], "mentions": [], "mention_roles": [], "pinned": False}
        raw.update(extra)
        g = self.channels[channel].get("guild_id")
        if g:
            raw["guild_id"] = g
        self.messages[channel][mid] = raw
        self.created[mid] = self.now
        self.channels[channel]["last_message_id"] = str(mid)
        return mid

    def delete(self, mid: int) -> None:
        for msgs in self.messages.values():
            if mid in msgs:
                del msgs[mid]
                self.deleted[mid] = self.now
                return

    def alive(self, channel: int) -> set[int]:
        return set(self.messages[channel])

    # -- the endpoints ----------------------------------------------------------------------

    def answer(self, path: str, q: dict[str, list[str]]):
        one = {k: v[-1] for k, v in q.items()}
        m = re.fullmatch(r"/api/v9/(.*)", path)
        if not m:
            return 404, {"message": "404: Not Found", "code": 0}
        p = m.group(1)
        if p == "users/@me":
            return 200, self.me
        if p == "users/@me/guilds":
            return 200, [{"id": g["id"], "name": g["name"], "approximate_member_count": g["approximate_member_count"]} for g in self.guilds.values()]
        if p == "users/@me/channels":
            return 200, [c for c in self.channels.values() if c["type"] in (1, 3)]
        if mm := re.fullmatch(r"guilds/(\d+)", p):
            g = self.guilds.get(int(mm.group(1)))
            return (200, {k: v for k, v in g.items() if k != "my_roles"}) if g else (404, {"message": "Unknown Guild", "code": 10004})
        if mm := re.fullmatch(r"users/@me/guilds/(\d+)/member", p):
            g = self.guilds[int(mm.group(1))]
            return 200, {"user": self.me, "roles": g["my_roles"], "nick": None}
        if mm := re.fullmatch(r"guilds/(\d+)/channels", p):
            gid = mm.group(1)
            return 200, [c for c in self.channels.values() if c.get("guild_id") == gid and c["type"] not in (10, 11, 12)]
        if mm := re.fullmatch(r"guilds/(\d+)/members/(\d+)", p):
            key = (int(mm.group(1)), int(mm.group(2)))
            if key in self.members:
                return 200, self.members[key]
            return 404, {"message": "Unknown Member", "code": 10007}
        if mm := re.fullmatch(r"channels/(\d+)", p):
            c = self.channels.get(int(mm.group(1)))
            return (200, c) if c else (404, {"message": "Unknown Channel", "code": 10003})
        if mm := re.fullmatch(r"channels/(\d+)/messages", p):
            return self.listing(int(mm.group(1)), one)
        if mm := re.fullmatch(r"channels/(\d+)/threads/search", p):
            return self.thread_search(int(mm.group(1)), one)
        if mm := re.fullmatch(r"guilds/(\d+)/messages/search", p):
            return self.search(q, guild=mm.group(1))
        if mm := re.fullmatch(r"channels/(\d+)/messages/search", p):
            return self.search(q, channel=int(mm.group(1)))
        return 404, {"message": "404: Not Found", "code": 0}

    def listing(self, channel: int, q: dict[str, str]):
        if channel in self.forbidden:
            return 403, {"message": "Missing Access", "code": 50001}
        if channel not in self.channels:
            return 404, {"message": "Unknown Channel", "code": 10003}
        limit = int(q.get("limit", 50))
        if not 1 <= limit <= 100:
            return 400, {"message": "Invalid Form Body", "code": 50035}
        ids = sorted(self.messages[channel])
        if "after" in q:              # Discord ignores `before` when `after` is present
            a = int(q["after"])
            chosen = [i for i in ids if i > a][:limit]
        elif "around" in q:
            r = int(q["around"])
            older = [i for i in ids if i < r][-(limit // 2):] if limit // 2 else []
            newer = [i for i in ids if i >= r][: limit - len(older)]
            chosen = older + newer
        elif "before" in q:
            b = int(q["before"])
            chosen = [i for i in ids if i < b][-limit:]
        else:
            chosen = ids[-limit:]
        return 200, [self.messages[channel][i] for i in sorted(chosen, reverse=True)]

    def thread_search(self, parent: int, q: dict[str, str]):
        archived = q.get("archived") == "true"
        ts = [self.channels[t] for t in self.threads.get(parent, []) if self.channels[t]["thread_metadata"]["archived"] == archived]
        ts.sort(key=lambda c: int(c.get("last_message_id") or c["id"]), reverse=True)
        off, lim = int(q.get("offset", 0)), int(q.get("limit", 25))
        page = ts[off: off + lim]
        firsts = [self.messages[int(t["id"])][int(t["id"])] for t in page if int(t["id"]) in self.messages[int(t["id"])]]
        return 200, {"threads": page, "members": [], "has_more": off + lim < len(ts), "total_results": len(ts), "first_messages": firsts}

    def search(self, q: dict[str, list[str]], *, guild: str | None = None, channel: int | None = None):
        one = {k: v[-1] for k, v in q.items()}
        limit = int(one.get("limit", 25))
        if limit > 25:
            return 400, {"message": "Invalid Form Body", "code": 50035}
        pool = []
        for cid, msgs in self.messages.items():
            c = self.channels[cid]
            if (guild and c.get("guild_id") != guild) or (channel and cid != channel) or cid in self.forbidden:
                continue
            pool += msgs.values()
        def keep(m: dict) -> bool:
            if "content" in one:
                words = set(re.findall(r"\w+", m["content"].lower()))
                if not set(re.findall(r"\w+", one["content"].lower())) <= words:
                    return False
            if "author_id" in q and m["author"]["id"] not in q["author_id"]:
                return False
            if "channel_id" in q and m["channel_id"] not in q["channel_id"]:
                return False
            if "min_id" in one and int(m["id"]) <= int(one["min_id"]):
                return False
            if "max_id" in one and int(m["id"]) >= int(one["max_id"]):
                return False
            if "mentions" in q and not {u["id"] for u in m.get("mentions", [])} & set(q["mentions"]):
                return False
            if one.get("pinned") == "true" and not m.get("pinned"):
                return False
            return True
        hits = sorted((m for m in pool if keep(m)), key=lambda m: int(m["id"]), reverse=one.get("sort_order", "desc") == "desc")
        off = int(one.get("offset", 0))
        return 200, {"total_results": len(hits), "messages": [[dict(m, hit=True)] for m in hits[off: off + limit]]}


class FakeDiscord:
    """The HTTP server. `script(path_regex, (status, body), …)` queues responses served before the world answers; `before_answer` runs before every answer, which is where a test changes the world between pages."""

    def __init__(self, world: ToyWorld | None = None, token: str = TOKEN) -> None:
        self.world = world or ToyWorld()
        self.token = token
        self.requests: list[dict] = []
        self.scripts: dict[str, deque] = defaultdict(deque)
        self.before_answer = None
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                u = urlparse(self.path)
                q = parse_qs(u.query, keep_blank_values=True)
                with fake.lock:
                    fake.requests.append({"method": self.command, "path": u.path, "query": q, "headers": dict(self.headers)})
                    status, body, headers = fake._respond(self.command, u.path, q, dict(self.headers))
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/api/v9"

    def __enter__(self) -> "FakeDiscord":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def script(self, pattern: str, *responses) -> None:
        self.scripts[pattern].extend(responses)

    def _respond(self, method: str, path: str, q, headers):
        if method != "GET":
            return 405, {"message": "405: Method Not Allowed", "code": 0}, None
        if not headers.get("User-Agent"):
            return 403, {"message": "no user agent"}, None
        if headers.get("Authorization") != self.token:
            return 401, {"message": "401: Unauthorized", "code": 0}, None
        for pattern, queue in self.scripts.items():
            if queue and re.fullmatch(pattern, path):
                r = queue.popleft()
                return (r[0], r[1], r[2] if len(r) > 2 else None)
        if self.before_answer:
            self.before_answer(path, q)
        status, body = self.world.answer(path, q)
        return status, body, None

    def calls(self, pattern: str) -> list[dict]:
        return [r for r in self.requests if re.fullmatch(pattern, r["path"])]
