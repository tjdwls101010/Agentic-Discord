"""`read`, `search` and `get`: a header with the cheap facts first, the whole result in a new file, and the transcript inline when it is small."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from discord import archive, directory, discord_api, transcript
from discord.errors import Refusal
from discord.records import READABLE_KINDS, Message

from .collect import Spec, collect


def _when(iso: str | None) -> str:
    if not iso:
        return "?"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")


def _bound_text(snowflake: int | None) -> str:
    return discord_api.time_of(snowflake).astimezone().strftime("%Y-%m-%d %H:%M:%S") if snowflake else "?"


def _label(db, channel) -> str:
    parts = []
    server = db.server_name(channel.server_id)
    if server:
        parts.append(server)
    if channel.parent_id:
        parent = db.channel(channel.parent_id)
        if parent and parent["name"]:
            parts.append(("#" if parent["kind"] not in ("category",) else "") + parent["name"])
    parts.append(("#" if channel.kind not in ("dm", "group_dm") else "") + (channel.name or str(channel.id)))
    return " › ".join(parts)


def _finish(results_dir: Path, command: str, header: list[str], body: str, count: int, inline: int, quiet: bool = False,
            unit: str = "messages", flag: str = "--inline {inline}") -> str:
    path = archive.write_result(results_dir, command, "\n".join(header) + "\n\n" + body + "\n")
    header = header + [f"result: {path}"]
    if quiet or not body:
        return "\n".join(header)
    if count > inline:
        return "\n".join(header + [f"(not printed: {count} {unit} > {flag.format(inline=inline)}; read the result file)"])
    return "\n".join(header) + "\n\n" + body


def _resume_args(channel_id: int, resume: dict) -> str:
    return f"read {channel_id} " + " ".join(f"--{k} {v}" for k, v in resume.items())


def read(api, db, results_dir: Path, channel_arg: str, *, last: int | None, after: str | None, before: str | None,
         around: str | None, new: bool, limit: int, skip_covered: bool, quiet: bool, inline: int) -> str:
    chosen = [n for n, v in (("--last", last), ("--around", around), ("--new", new or None)) if v is not None]
    if len(chosen) > 1 or (chosen and (after or before)):
        raise Refusal("choose one range: --last N, --after X [--before Y], --before Y, --around M, or --new", kind="usage")
    if limit < 1 or (last is not None and last < 1):
        raise Refusal("--limit and --last must be at least 1", kind="usage")
    if around and skip_covered:
        raise Refusal("--skip-covered does not apply to --around (one window)", kind="usage")
    channel = directory.resolve_channel(api, db, channel_arg)
    if channel.kind == "forum":
        raise Refusal(f"#{channel.name} is a forum: its messages live in its posts", fix=f"list them with `threads {channel.id}`, then read a post by its id")
    if channel.kind == "category":
        raise Refusal(f"{channel.name} is a category, which holds channels, not messages", fix="run `channels <server>` and pick a channel")
    if channel.can_view is False or channel.can_read_history is False:
        raise Refusal(f"your roles do not let you read #{channel.name}'s history (as worked out when the channels were last listed)",
                      fix="run `channels <server>` again if your roles changed", kind="forbidden")
    if channel.kind not in READABLE_KINDS and channel.kind not in ("other", "unknown"):
        raise Refusal(f"{channel.kind} channels hold no messages to read")
    lo = discord_api.parse_bound(after) if after else None
    hi = discord_api.parse_bound(before) if before else None
    if lo is not None and hi is not None and lo >= hi:
        raise Refusal("--after must be earlier than --before", kind="usage")
    if around:
        spec = Spec("around", limit, around=discord_api.parse_bound(around))
        what = f"around [{spec.around}] ({_bound_text(spec.around)})"
    elif new:
        spec = Spec("new", limit, skip_covered=skip_covered)
        what = "new since this archive last reached the newest message"
    elif lo is not None:
        spec = Spec("after", limit, after=lo, before=hi, skip_covered=skip_covered)
        what = f"after {_bound_text(lo)}" + (f" and before {_bound_text(hi)}" if hi else "") + ", oldest first"
    elif hi is not None:
        spec = Spec("before", limit, before=hi, skip_covered=skip_covered)
        what = f"before {_bound_text(hi)}, newest first"
    else:
        spec = Spec("last", last or limit, skip_covered=skip_covered)
        what = f"the newest {last or limit}"
    out = collect(api, db, channel.id, spec, last_message_id=channel.last_message_id)
    msgs = out.messages
    names = directory.names_for(api, db, channel.server_id, msgs, look_up=not quiet)
    body = transcript.render(msgs, names)
    got = f"got: {len(msgs)} messages"
    if msgs:
        got += f" ({_when(msgs[0].sent_at)} → {_when(msgs[-1].sent_at)})"
    got += f" in {out.requests} request{'s' if out.requests != 1 else ''}"
    if out.from_archive:
        got += f"; {out.from_archive} of them from the archive (--skip-covered)"
    if spec.mode == "around":
        end = (f"one window; continue with `{_resume_args(channel.id, out.resume['older'])}` or `{_resume_args(channel.id, out.resume['newer'])}`"
               if out.resume else "one window; nothing there")
    elif out.cut == "newer":
        end = (f"cut at --limit {limit}: newer messages remain; continue with `read {channel.id} --new`" if spec.mode == "new" else
               f"cut at --limit {limit}: newer messages remain; continue with `{_resume_args(channel.id, out.resume)}`")
    elif out.cut == "older":
        end = f"older messages remain; continue with `{_resume_args(channel.id, out.resume)}`"
    elif out.reached_start:
        end = "reached the channel's first message" + ("; also its newest" if out.reached_head else "")
    elif out.reached_head:
        end = "reached the newest message"
    else:
        end = "complete"
    header = [f"read: {_label(db, channel)}  ({channel.kind}) [{channel.id}]", f"range: {what}", got, f"end: {end}"]
    return _finish(results_dir, "read", header, body, len(msgs), inline, quiet)


_SORT = {"newest": ("timestamp", "desc"), "oldest": ("timestamp", "asc"), "relevance": ("relevance", "desc")}


def search(api, db, results_dir: Path, target: str, *, content: str | None, authors: list[str], mentions: list[str],
           channels: list[str], has: list[str], after: str | None, before: str | None, pinned: bool, sort: str,
           offset: int, limit: int, count: bool, inline: int) -> str:
    if offset < 0 or offset > discord_api.SEARCH_MAX_OFFSET:
        raise Refusal(f"--offset must be between 0 and {discord_api.SEARCH_MAX_OFFSET}: Discord pages no deeper", kind="usage")
    if limit < 1:
        raise Refusal("--limit must be at least 1", kind="usage")
    kind, tid, tname = directory.resolve_target(api, db, target)
    server_id = tid if kind == "server" else None
    query: list[tuple[str, str]] = []
    shown: list[str] = []
    if content:
        query.append(("content", content))
        shown.append(f"content \"{content}\"")
    for a in authors:
        uid = directory.resolve_user(db, server_id, a)
        query.append(("author_id", str(uid)))
        shown.append(f"from {a}")
    for m in mentions:
        uid = directory.resolve_user(db, server_id, m)
        query.append(("mentions", str(uid)))
        shown.append(f"mentions {m}")
    for c in channels:
        ch = directory.resolve_channel(api, db, c if kind != "server" or "/" in c or c.isdigit() or "discord" in c else f"{tid}/{c}")
        query.append(("channel_id", str(ch.id)))
        shown.append(f"in #{ch.name or ch.id}")
    for h in has:
        query.append(("has", h))
        shown.append(f"has {h}")
    if after:
        query.append(("min_id", str(discord_api.parse_bound(after))))
        shown.append(f"after {after}")
    if before:
        query.append(("max_id", str(discord_api.parse_bound(before))))
        shown.append(f"before {before}")
    if pinned:
        query.append(("pinned", "true"))
        shown.append("pinned")
    by, order = _SORT[sort]
    query += [("sort_by", by), ("sort_order", order)]
    call = dict(server_id=tid) if kind == "server" else dict(channel_id=tid)
    head = f"search: {tname}" + ("  · " + " · ".join(shown) if shown else "")
    if count:
        total, _ = api.search(**call, query=query + [("offset", str(offset)), ("limit", "1")])
        return "\n".join([head, f"total: ~{total} (Discord's estimate; it can be off by a few)"])
    hits: list[Message] = []
    total = None
    at = offset
    while len(hits) < limit and at <= discord_api.SEARCH_MAX_OFFSET:
        want = min(discord_api.SEARCH_PAGE, limit - len(hits))
        total, page = api.search(**call, query=query + [("offset", str(at)), ("limit", str(want))])
        hits += page
        at += len(page)
        if len(page) < want:
            break
    db.add_strays(hits, archive.now())
    directory.name_channels(api, db, server_id, {m.channel_id for m in hits})
    names = directory.names_for(api, db, server_id, hits, look_up=True)
    body = transcript.render(hits, names, show_channel=True)
    more = total is not None and at < total and at <= discord_api.SEARCH_MAX_OFFSET
    header = [head, f"total: ~{total} (Discord's estimate) · hits {offset + 1 if hits else 0}–{offset + len(hits)}, {sort} first"
              + (f" · next page: --offset {at}" if more else " · no more hits")]
    return _finish(results_dir, "search", header, body, len(hits), inline)


_API_PREFIX = "/api/v9/"


def _messages_in(data) -> list[dict]:
    """Message objects anywhere in a response: a list of them, `messages` (also nested lists), pins' `items[].message`, or one message."""
    def is_message(x) -> bool:
        return isinstance(x, dict) and {"id", "channel_id", "author", "timestamp"} <= x.keys()
    found: list[dict] = []

    def walk(x, depth: int = 0) -> None:
        if depth > 3:
            return
        if is_message(x):
            found.append(x)
        elif isinstance(x, list):
            for y in x:
                walk(y, depth + 1)
        elif isinstance(x, dict):
            for k in ("messages", "items", "message", "first_messages"):
                if k in x:
                    walk(x[k], depth + 1)
    walk(data)
    unique = {int(m["id"]): m for m in found}
    return [unique[k] for k in sorted(unique)]


def get(api, db, results_dir: Path, path: str, query: list[tuple[str, str]], *, transcript_view: bool, inline: int) -> str:
    if "://" in path:
        if _API_PREFIX not in path:
            raise Refusal("give a path under the API, such as users/@me/mentions", kind="usage")
        path = path.split(_API_PREFIX, 1)[1]
    path = path.lstrip("/")
    if ".." in path.split("/"):
        raise Refusal("the path may not contain ..", kind="usage")
    data = api.get(path, query or None)
    shown = path + ("?" + "&".join(f"{k}={v}" for k, v in query) if query else "")
    if transcript_view:
        raws = _messages_in(data)
        msgs = [discord_api.parse_message(m) for m in raws]
        db.add_strays(msgs, archive.now())
        server_ids = {m.server_id for m in msgs if m.server_id}
        names = directory.names_for(api, db, server_ids.pop() if len(server_ids) == 1 else None, msgs, look_up=False)
        body = transcript.render(msgs, names, show_channel=True)
        header = [f"get: {shown}", f"messages: {len(msgs)} found in the response (stored as stray rows)"]
        return _finish(results_dir, "get", header, body, len(msgs), inline)
    body = json.dumps(data, ensure_ascii=False, indent=1)
    header = [f"get: {shown}", f"json: {len(body)} characters"]
    return _finish(results_dir, "get", header, body, len(body), inline * 1000, unit="characters", flag="--inline × 1000 = {inline}")
