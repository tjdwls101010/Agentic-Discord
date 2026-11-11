"""The archive database: opening it, and every write that has to keep messages and coverage consistent."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from discord.errors import Refusal
from discord.records import Channel, Message, Server, User

from .coverage import Page, prove

SCHEMA = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Range:
    lo: int
    hi: int
    oldest_fetched_at: str
    head_at: str | None


def _b(v: bool | None) -> int | None:
    return None if v is None else int(bool(v))


class Archive:
    """One writing connection to the archive. Every method that writes commits on its own, as one transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists() or path.stat().st_size == 0
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.db.execute("PRAGMA busy_timeout = 5000")
        self.db.execute("PRAGMA foreign_keys = ON")
        if fresh:
            self.db.executescript("BEGIN;" + SCHEMA.read_text(encoding="utf-8") + "COMMIT;")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            self.db.close()
            raise Refusal(f"the archive at {path} has schema version {version}; this version of the skill writes only version {SCHEMA_VERSION}",
                          fix=f"move {path} aside to start a new archive; the old one stays readable with sqlite3", kind="failed")
        self.db.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        self.db.close()

    # -- transactions -------------------------------------------------------------------------

    def _tx(self):
        db = self.db

        class _T:
            def __enter__(self_inner):
                db.execute("BEGIN IMMEDIATE")
                return db

            def __exit__(self_inner, exc_type, exc, tb):
                db.execute("COMMIT" if exc_type is None else "ROLLBACK")
                return False
        return _T()

    # -- directory writes -----------------------------------------------------------------------

    def save_server(self, server: Server, fetched_at: str) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO servers (id, name, fetched_at) VALUES (?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET name=excluded.name, fetched_at=excluded.fetched_at",
                       (server.id, server.name, fetched_at))
            for rid, name in server.roles.items():
                db.execute("INSERT INTO roles (id, server_id, name) VALUES (?,?,?) "
                           "ON CONFLICT(id) DO UPDATE SET name=excluded.name", (rid, server.id, name))

    def save_channels(self, channels: list[Channel], fetched_at: str) -> None:
        """Upsert channels; parents first, so a thread's parent row exists before the thread's."""
        with self._tx() as db:
            pending = list(channels)
            known = {c.id for c in channels}
            ordered: list[Channel] = []
            placed: set[int] = set()
            while pending:
                rest = []
                for c in pending:
                    if c.parent_id is None or c.parent_id in placed or c.parent_id not in known:
                        ordered.append(c)
                        placed.add(c.id)
                    else:
                        rest.append(c)
                if len(rest) == len(pending):
                    ordered += rest
                    break
                pending = rest
            for c in ordered:
                self._upsert_channel(db, c, fetched_at)

    def _server_known(self, db, server_id: int | None) -> int | None:
        if server_id is None:
            return None
        return server_id if db.execute("SELECT 1 FROM servers WHERE id=?", (server_id,)).fetchone() else None

    def _upsert_channel(self, db, c: Channel, fetched_at: str) -> None:
        parent = c.parent_id
        if parent is not None and not db.execute("SELECT 1 FROM channels WHERE id=?", (parent,)).fetchone():
            self._placeholder_channel(db, parent, c.server_id, fetched_at)
        db.execute(
            "INSERT INTO channels (id, server_id, parent_id, kind, name, topic, is_archived, can_view, can_read_history, last_message_id, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET server_id=excluded.server_id, parent_id=excluded.parent_id, "
            "kind=excluded.kind, name=excluded.name, topic=excluded.topic, is_archived=excluded.is_archived, "
            "can_view=COALESCE(excluded.can_view, channels.can_view), can_read_history=COALESCE(excluded.can_read_history, channels.can_read_history), "
            "last_message_id=COALESCE(excluded.last_message_id, channels.last_message_id), fetched_at=excluded.fetched_at",
            (c.id, self._server_known(db, c.server_id), parent, c.kind, c.name, c.topic, int(c.is_archived),
             _b(c.can_view), _b(c.can_read_history), c.last_message_id, fetched_at))

    def _placeholder_channel(self, db, channel_id: int, server_id: int | None, fetched_at: str) -> None:
        db.execute("INSERT INTO channels (id, server_id, kind, fetched_at) VALUES (?,?,'unknown',?) ON CONFLICT(id) DO NOTHING",
                   (channel_id, self._server_known(db, server_id), fetched_at))

    def save_members(self, server_id: int, found: list[tuple[int, User | None, str | None, bool]], fetched_at: str) -> None:
        """(user id, user as the lookup returned it or None, nick, is_member) for each lookup that got an answer. A user the lookup did not return must already be stored."""
        with self._tx() as db:
            for user_id, user, nick, is_member in found:
                if user is not None:
                    self._upsert_user(db, user, fetched_at)
                elif not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                    continue
                db.execute("INSERT INTO members (server_id, user_id, nick, is_member, fetched_at) VALUES (?,?,?,?,?) "
                           "ON CONFLICT(server_id, user_id) DO UPDATE SET nick=excluded.nick, is_member=excluded.is_member, fetched_at=excluded.fetched_at",
                           (server_id, user_id, nick, int(is_member), fetched_at))

    def save_users(self, users: list[User], fetched_at: str) -> None:
        with self._tx() as db:
            for u in users:
                self._upsert_user(db, u, fetched_at)

    def _upsert_user(self, db, u: User, fetched_at: str) -> None:
        db.execute("INSERT INTO users (id, username, global_name, is_bot, fetched_at) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET username=excluded.username, global_name=excluded.global_name, "
                   "is_bot=excluded.is_bot, fetched_at=excluded.fetched_at WHERE excluded.fetched_at >= users.fetched_at",
                   (u.id, u.username, u.global_name, int(u.is_bot), fetched_at))

    # -- message writes -------------------------------------------------------------------------

    def _message_row(self, m: Message, fetched_at: str) -> tuple:
        return (m.id, m.channel_id, m.author.id, m.sent_at, m.edited_at, m.kind, m.content, m.reply_to_id,
                json.dumps(m.raw, ensure_ascii=False, separators=(",", ":")), fetched_at)

    def _prepare(self, db, messages: list[Message], fetched_at: str) -> None:
        """Rows a message's foreign keys need: everyone it names, and a placeholder for a channel never listed."""
        for m in messages:
            for u in m.people():
                self._upsert_user(db, u, fetched_at)
            if not db.execute("SELECT 1 FROM channels WHERE id=?", (m.channel_id,)).fetchone():
                self._placeholder_channel(db, m.channel_id, m.server_id, fetched_at)

    def _insert_strays(self, db, messages: list[Message], fetched_at: str) -> None:
        """Messages from outside a read-through range: stored only when no row exists, and no existing row is touched, because these paths do not prove the message exists now."""
        self._prepare(db, messages, fetched_at)
        for m in messages:
            db.execute("INSERT INTO messages (id, channel_id, author_id, sent_at, edited_at, kind, content, reply_to_id, raw, fetched_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING", self._message_row(m, fetched_at))

    def add_strays(self, messages: list[Message], fetched_at: str) -> None:
        """Search hits and messages from a raw GET, with what they reference."""
        with self._tx() as db:
            refs = [m.referenced for m in messages if m.referenced]
            self._insert_strays(db, list(messages) + refs, fetched_at)

    def record_page(self, page: Page) -> tuple[int, int] | None:
        """Store one page of a channel's listing and what it proves, in one transaction: the messages, the range read through (merged with ranges it overlaps or touches), absences inside that range, and the head mark. Returns the range proven, if any."""
        proof, reaches_head = prove(page)
        with self._tx() as db:
            if not db.execute("SELECT 1 FROM channels WHERE id=?", (page.channel_id,)).fetchone():
                self._placeholder_channel(db, page.channel_id, None, page.fetched_at)
            self._prepare(db, list(page.messages), page.fetched_at)
            for m in page.messages:
                row = self._message_row(m, page.fetched_at)
                db.execute("INSERT INTO messages (id, channel_id, author_id, sent_at, edited_at, kind, content, reply_to_id, raw, fetched_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET channel_id=excluded.channel_id, "
                           "author_id=excluded.author_id, sent_at=excluded.sent_at, edited_at=excluded.edited_at, kind=excluded.kind, "
                           "content=excluded.content, reply_to_id=excluded.reply_to_id, raw=excluded.raw, fetched_at=excluded.fetched_at, "
                           "absent_at=NULL WHERE excluded.fetched_at >= messages.fetched_at", row)
            self._insert_strays(db, [m.referenced for m in page.messages if m.referenced], page.fetched_at)
            if proof is not None:
                seen = [m.id for m in page.messages]
                # Only rows already stored when this page was requested can be missing from it; a row another run stored since may be newer than this answer.
                db.execute(f"UPDATE messages SET absent_at=? WHERE channel_id=? AND id BETWEEN ? AND ? AND absent_at IS NULL "
                           f"AND fetched_at < ? AND id NOT IN ({','.join('?' * len(seen))})",
                           (page.fetched_at, page.channel_id, proof.lo, proof.hi, page.fetched_at, *seen))
                self._merge(db, page.channel_id, proof.lo, proof.hi, page.fetched_at, page.fetched_at if proof.reaches_head else None)
            elif reaches_head and page.after is not None:
                # Nothing after `after`: the range holding it reached the newest message.
                row = db.execute("SELECT lo_id FROM coverage WHERE channel_id=? AND lo_id <= ? AND hi_id >= ?",
                                 (page.channel_id, page.after, page.after)).fetchone()
                if row:
                    db.execute("UPDATE coverage SET head_at=NULL WHERE channel_id=? AND lo_id != ?", (page.channel_id, row[0]))
                    db.execute("UPDATE coverage SET head_at=? WHERE channel_id=? AND lo_id=?", (page.fetched_at, page.channel_id, row[0]))
        return (proof.lo, proof.hi) if proof else None

    def _merge(self, db, channel_id: int, lo: int, hi: int, fetched_at: str, head_at: str | None) -> None:
        touching = db.execute("SELECT lo_id, hi_id, oldest_fetched_at, head_at FROM coverage "
                              "WHERE channel_id=? AND lo_id <= ? AND hi_id >= ?", (channel_id, hi + 1, lo - 1)).fetchall()
        new_lo = min([lo] + [r[0] for r in touching])
        new_hi = max([hi] + [r[1] for r in touching])
        oldest = min([fetched_at] + [r[2] for r in touching])
        heads = [r[3] for r in touching if r[3]]
        db.execute("DELETE FROM coverage WHERE channel_id=? AND lo_id <= ? AND hi_id >= ?", (channel_id, hi + 1, lo - 1))
        if head_at is not None:
            db.execute("UPDATE coverage SET head_at=NULL WHERE channel_id=?", (channel_id,))
        else:
            head_at = max(heads) if heads else None
        db.execute("INSERT INTO coverage (channel_id, lo_id, hi_id, oldest_fetched_at, head_at) VALUES (?,?,?,?,?)",
                   (channel_id, new_lo, new_hi, oldest, head_at))

    # -- reads ------------------------------------------------------------------------------------

    def coverage(self, channel_id: int) -> list[Range]:
        return [Range(*r) for r in self.db.execute(
            "SELECT lo_id, hi_id, oldest_fetched_at, head_at FROM coverage WHERE channel_id=? ORDER BY lo_id", (channel_id,))]

    def head(self, channel_id: int) -> Range | None:
        r = self.db.execute("SELECT lo_id, hi_id, oldest_fetched_at, head_at FROM coverage WHERE channel_id=? AND head_at IS NOT NULL",
                            (channel_id,)).fetchone()
        return Range(*r) if r else None

    def stored_raw(self, channel_id: int, lo: int, hi: int) -> list[dict]:
        """Raw message objects stored for `channel_id` in [lo, hi] and not marked absent, oldest first."""
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT raw FROM messages WHERE channel_id=? AND id BETWEEN ? AND ? AND absent_at IS NULL ORDER BY id", (channel_id, lo, hi))]

    def channel(self, channel_id: int) -> dict | None:
        r = self.db.execute("SELECT id, server_id, parent_id, kind, name, last_message_id, can_view, can_read_history FROM channels WHERE id=?",
                            (channel_id,)).fetchone()
        if not r:
            return None
        keys = ("id", "server_id", "parent_id", "kind", "name", "last_message_id", "can_view", "can_read_history")
        return dict(zip(keys, r))

    def names(self, server_id: int | None, user_ids: set[int], channel_ids: set[int], role_ids: set[int]) -> tuple[dict, dict, dict]:
        """Cached display names: users (nick in `server_id`, else global name, else username), channels and roles. Ids not stored are left out."""
        users: dict[int, str] = {}
        for uid in user_ids:
            r = self.db.execute("SELECT COALESCE(mb.nick, u.global_name, u.username) FROM users u "
                                "LEFT JOIN members mb ON mb.user_id=u.id AND mb.server_id IS ? WHERE u.id=?", (server_id, uid)).fetchone()
            if r:
                users[uid] = r[0]
        channels = {cid: r[0] for cid in channel_ids
                    if (r := self.db.execute("SELECT name FROM channels WHERE id=? AND name IS NOT NULL", (cid,)).fetchone())}
        roles = {rid: r[0] for rid in role_ids if (r := self.db.execute("SELECT name FROM roles WHERE id=?", (rid,)).fetchone())}
        return users, channels, roles

    def members_to_look_up(self, server_id: int, user_ids: set[int], fresh_after: str) -> set[int]:
        """Of `user_ids`, those with no member row in `server_id` fetched at or after `fresh_after`."""
        out = set()
        for uid in user_ids:
            r = self.db.execute("SELECT fetched_at FROM members WHERE server_id=? AND user_id=?", (server_id, uid)).fetchone()
            if not r or r[0] < fresh_after:
                out.add(uid)
        return out

    def size(self) -> int:
        return sum(p.stat().st_size for p in (self.path, self.path.with_name(self.path.name + "-wal")) if p.exists())
