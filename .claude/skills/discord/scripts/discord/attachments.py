"""`files`: downloading what messages in the archive have attached into the working directory, getting a message again from Discord when its links expired.

What a call costs is most of the logic: a file already on disk costs nothing; a live link costs one CDN request and no token (the token is not even read); an expired link costs an API request for its message, through the collector as a 3-message `around` window so the archive learns from it as from any read, at most `refresh` messages per call.

Names and ids come from other people's messages, so the path is a trust boundary: only numeric ids name directories and files, a filename is cleaned before use, nothing is written through a symbolic link, and a file appears at its final path only once all of it arrived.
"""

from __future__ import annotations

import os
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from discord import archive, discord_api, reading
from discord.errors import Refusal
from discord.records import Attachment, Message
from discord.transcript import size_text

#: A link this close to its expiry is treated as expired: it could lapse before the request lands.
EXPIRY_MARGIN = timedelta(seconds=60)
#: The window fetched around a message whose links expired: it and one neighbour each side, so a message gone missing is marked absent by the ordinary page rule.
WINDOW = 3
#: The longest file name most file systems take, in bytes.
NAME_BYTES = 255
#: The longest extension kept whole when a name is shortened, in bytes.
EXTENSION_BYTES = 16


@dataclass
class _File:
    message: Message          # the message these files are listed under (the original, for a thread's first entry)
    attachment: Attachment
    path: Path | None = None
    shown: str | None = None  # the listing line, once the file is on disk
    reason: str | None = None # why it is not, otherwise
    new_bytes: int = 0        # bytes downloaded by this call
    downloaded: bool = False


def _attached(m: Message) -> tuple[Attachment, ...]:
    return m.attachments + (m.forward.attachments if m.forward else ())


def safe_name(attachment_id: int, filename: str) -> str:
    """`<attachment id>-<filename>` safe to create in one directory: no path separators, control characters or lone surrogates, no leading dots, `file` when nothing is left, and at most NAME_BYTES bytes of UTF-8, shortened in the stem so the extension survives."""
    name = "".join(c for c in filename if c not in "/\\" and unicodedata.category(c) not in ("Cc", "Cs")).lstrip(".") or "file"
    prefix = f"{attachment_id}-"
    room = NAME_BYTES - len(prefix.encode())
    if len(name.encode()) > room:
        stem, ext = os.path.splitext(name)
        if not ext or len(ext.encode()) > EXTENSION_BYTES:
            stem, ext = name, ""
        stem = stem.encode()[: room - len(ext.encode())].decode("utf-8", "ignore") or "file"
        name = stem + ext
    return prefix + name


def _expired(a: Attachment, now: datetime) -> bool:
    if not a.expires_at:
        return False
    return datetime.fromisoformat(a.expires_at.replace("Z", "+00:00")) <= now + EXPIRY_MARGIN


class _Disk:
    """Where files go, and writing one there without following a symbolic link or leaving a partial file behind."""

    def __init__(self, output_root: Path) -> None:
        self.output = output_root
        self.root = output_root / "files"

    def place(self, message_id: int, a: Attachment) -> Path | str:
        """The final path for `a`, or why there is none."""
        if not all(isinstance(i, int) and 0 < i < 1 << 64 for i in (a.id, message_id)):
            return "its id is not a snowflake (a plain number of at most 20 digits), so it cannot name a file safely"
        folder = self.root / str(message_id)
        path = folder / safe_name(a.id, a.filename)
        for p in (self.output, self.root, folder, path):
            if p.is_symlink():
                return f"{p} is a symbolic link; nothing is written through one"
        return path

    def save(self, url: str, path: Path, max_bytes: int) -> discord_api.Download:
        """Download `url` to a temporary file beside `path` and move it to `path` only when all of it arrived. A file system error fails this file alone."""
        try:
            return self._save(url, path, max_bytes)
        except OSError as e:
            return discord_api.Download("failed", detail=f"could not write it here ({e.strerror or type(e).__name__})")

    def _save(self, url: str, path: Path, max_bytes: int) -> discord_api.Download:
        archive.prepare_output(self.output)
        for d in (self.root, path.parent):
            d.mkdir(exist_ok=True)
            if d.is_symlink():
                return discord_api.Download("failed", detail=f"{d} is a symbolic link; nothing is written through one")
        if Path(os.path.realpath(path.parent)) != Path(os.path.realpath(self.root)) / path.parent.name:
            return discord_api.Download("failed", detail=f"{path.parent} does not lie where it should")
        tmp = path.parent / f".{path.name.split('-', 1)[0]}-{secrets.token_hex(4)}.part"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        kept = False
        try:
            with os.fdopen(fd, "wb") as f:
                d = discord_api.download(url, f, max_bytes=max_bytes)
            if d.outcome == "saved":
                os.replace(tmp, path)
                kept = True
            return d
        finally:
            if not kept:
                tmp.unlink(missing_ok=True)


def files(db, connect, output_root: Path, ids: list[int], *, max_bytes: int, refresh: int, now: datetime | None = None) -> str:
    """Download the attachments of the stored messages `ids` under `output_root`/files and return the listing. `connect()` gives the API; it is called only when a link expired, so live links need no token."""
    now = now or datetime.now(timezone.utc)
    found, missing = db.messages_by_id(ids)
    if not found:
        raise Refusal(f"none of the {len(missing)} ids is in the archive",
                      fix="fetch the messages first (`read`, `search`, `get --transcript`), or check the query that chose them")
    notes = [f"[{i}] not in the archive" for i in missing]
    messages: dict[int, Message] = {}
    for s in found:
        m = discord_api.parse_message(s.raw, channel_id=s.channel_id)
        if m.kind == "thread_starter":
            if m.referenced is None:
                notes.append(f"[{m.id}] the first entry of a thread whose original message is not known (deleted, or not sent along)")
                continue
            own, _ = db.messages_by_id([m.referenced.id])
            m = discord_api.parse_message(own[0].raw, channel_id=own[0].channel_id) if own else m.referenced
        messages.setdefault(m.id, m)
    disk = _Disk(output_root)
    todo: list[_File] = []
    for mid in sorted(messages):
        m = messages[mid]
        attached = _attached(m)
        if not attached:
            notes.append(f"[{mid}] no attachments")
        todo += [_File(m, a) for a in attached]

    def take(f: _File, a: Attachment) -> bool:
        """Try `a`'s link for `f`; True when the link turned out expired and the message must be fetched again."""
        d = disk.save(a.url, f.path, max_bytes)
        if d.outcome == "saved":
            f.new_bytes, f.downloaded = d.size, True
            dims = f"{a.width}×{a.height}" if a.width and a.height else ""
            f.shown = f"  {f.path}  " + " ".join(x for x in (d.content_type, size_text(d.size), dims) if x)
            return False
        if d.outcome == "expired":
            return True
        f.reason = {"too_large": f"over --max-mb ({d.detail})", "refused": f"its link is not Discord's CDN ({d.detail})"}.get(d.outcome) \
            or f"download failed: {d.detail}"
        return False

    pending: dict[int, list[_File]] = {}
    for f in todo:
        a = f.attachment
        place = disk.place(f.message.id, a)
        if isinstance(place, str):
            f.reason = place
            continue
        f.path = place
        if place.is_file():
            dims = f" {a.width}×{a.height}" if a.width and a.height else ""
            f.shown = f"  {place}  {size_text(place.stat().st_size)}{dims} (already here)"
        elif a.size is not None and a.size > max_bytes:
            f.reason = f"over --max-mb (declared {size_text(a.size)})"
        elif _expired(a, now) or take(f, a):
            pending.setdefault(f.message.id, []).append(f)

    api = None
    stopped: Refusal | None = None
    tried = refreshed = requests = 0
    over: dict[int, list[int]] = {}
    for mid in sorted(pending):
        group = pending[mid]
        m = group[0].message
        if stopped is not None:
            for f in group:
                f.reason = "link expired; not refreshed (refreshing stopped, see below)"
            continue
        if tried >= refresh:
            for f in group:
                f.reason = f"link expired; not refreshed (--refresh {refresh} reached)"
            over.setdefault(m.channel_id, []).append(mid)
            continue
        try:
            if api is None:
                api = connect()
            tried += 1
            before = api.requests
            try:
                out = reading.collect(api, db, m.channel_id, reading.Spec("around", WINDOW, around=mid))
            finally:
                requests += api.requests - before
            refreshed += 1
        except discord_api.HttpFailure as e:
            if e.status in (403, 404):
                why = "access refused" if e.status == 403 else "not found"
                for f in group:
                    f.reason = f"link expired; getting the message again failed: {why} ({e.message})"
                continue
            stopped = e
        except Refusal as e:
            stopped = e
        if stopped is not None:
            for f in group:
                f.reason = "link expired; not refreshed (refreshing stopped, see below)"
            continue
        fresh = next((x for x in out.messages if x.id == mid), None)
        if fresh is None:
            for f in group:
                f.reason = "link expired, and the message is not in the refreshed answer (deleted, or no longer visible to you; not proof of deletion)"
            continue
        now_attached = {a.id: a for a in _attached(fresh)}
        for f in group:
            a = now_attached.get(f.attachment.id)
            if a is None:
                f.reason = "the message no longer has this attachment"
            elif take(f, a):
                f.reason = "the link was expired again right after refreshing"

    return _listing(db, messages, todo, notes, over, stopped, refreshed, requests)


def _who_where(db, m: Message) -> str:
    row = db.channel(m.channel_id) or {}
    users, channels, _ = db.names(row.get("server_id"), {m.author.id}, {m.channel_id}, set())
    author = users.get(m.author.id) or m.author.global_name or m.author.username
    channel = channels.get(m.channel_id) or str(m.channel_id)
    mark = "" if row.get("kind") in ("dm", "group_dm") else "#"
    when = datetime.fromisoformat(m.sent_at.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M") if m.sent_at else "?"
    return f"[{m.id}] {when} {author} · {mark}{channel}"


def _listing(db, messages: dict[int, Message], todo: list[_File], notes: list[str], over: dict[int, list[int]],
             stopped: Refusal | None, refreshed: int, requests: int) -> str:
    new = [f for f in todo if f.downloaded]
    here = [f for f in todo if f.shown and not f.downloaded]
    lines = [f"files: {len(messages)} messages, {len(todo)} files · downloaded {len(new)} ({size_text(sum(f.new_bytes for f in new))})"
             f" · already here {len(here)} · fetched again {refreshed} messages in {requests} API requests"]
    for mid in sorted(messages):
        shown = [f.shown for f in todo if f.message.id == mid and f.shown]
        if shown:
            lines += [_who_where(db, messages[mid])] + shown
    failed = [f"[{f.message.id}] {f.attachment.filename or '(no name)'}: {f.reason}" for f in todo if f.reason]
    if failed or notes or over or stopped:
        lines.append("not downloaded:")
        lines += [f"  {x}" for x in notes + failed]
        for channel_id, mids in sorted(over.items()):
            lines.append(f"  to refresh those links, read their range again (100 messages per request; if the read stops at its --limit, its header gives the arguments that continue it), then run files again: "
                         f"read {channel_id} --after {min(mids) - 1} --before {max(mids) + 1}")
        if stopped is not None:
            lines.append(f"  refreshing stopped: {stopped.message}" + (f"; fix: {stopped.fix}" if stopped.fix else ""))
    return "\n".join(lines)
