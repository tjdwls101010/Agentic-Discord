"""`servers`, `channels` and `threads`: what you can see, as compact text, saved to the archive on the way."""

from __future__ import annotations

from discord import archive, discord_api
from discord.records import Channel

from .resolve import resolve_channel, resolve_server

_TOPIC = 80


def _when(snowflake: int | None) -> str:
    if not snowflake:
        return "none"
    return discord_api.time_of(snowflake).astimezone().strftime("%Y-%m-%d %H:%M")


def _cut(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def servers_text(api, db) -> str:
    now = archive.now()
    servers = api.servers()
    for s in servers:
        db.save_server(s, now)
    dms = api.dms()
    db.save_channels(dms, now)
    lines = [f"servers ({len(servers)}):"]
    lines += [f"  {s.name}  [{s.id}]" + (f"  · {s.member_count} members" if s.member_count else "") for s in sorted(servers, key=lambda s: s.name.casefold())]
    lines.append(f"DMs ({len(dms)}):")
    for c in sorted(dms, key=lambda c: c.last_message_id or 0, reverse=True):
        kind = "group " if c.kind == "group_dm" else ""
        lines.append(f"  {kind}{c.name or '(no name)'}  [{c.id}]  · last message {_when(c.last_message_id)}")
    return "\n".join(lines)


def channels_text(api, db, server_arg: str) -> str:
    sid, _ = resolve_server(api, db, server_arg)
    now = archive.now()
    server, channels = api.server_channels(sid)
    db.save_server(server, now)
    db.save_channels(channels, now)
    by_id = {c.id: c for c in channels}
    visible = [c for c in channels if c.can_view and c.kind != "category"]
    hidden = sum(1 for c in channels if not c.can_view and c.kind != "category")
    groups: dict[int | None, list[Channel]] = {}
    for c in visible:
        groups.setdefault(c.parent_id if c.parent_id in by_id else None, []).append(c)
    lines = [f"{server.name}  [{server.id}]  · {len(visible)} channels you can see, {hidden} hidden from you (not listed)"]

    def line(c: Channel) -> str:
        bits = [f"{c.kind}", f"last message {_when(c.last_message_id)}"]
        if c.kind == "forum":
            bits.append("posts: use threads")
        elif not c.can_read_history:
            bits.append("history not readable")
        topic = f"  — {_cut(c.topic, _TOPIC)}" if c.topic else ""
        return f"#{c.name}  [{c.id}]  · " + " · ".join(bits) + topic

    for c in sorted(groups.get(None, []), key=lambda c: c.position):
        lines.append("  " + line(c))
    for cat in sorted((by_id[k] for k in groups if k is not None), key=lambda c: c.position):
        lines.append(f"{cat.name}/")
        lines += ["  " + line(c) for c in sorted(groups[cat.id], key=lambda c: c.position)]
    return "\n".join(lines)


def threads_text(api, db, channel_arg: str, *, limit: int) -> str:
    parent = resolve_channel(api, db, channel_arg)
    seen: dict[int, Channel] = {}
    totals = {}
    firsts = {}
    for archived in (False, True):
        offset = 0
        listed = 0
        while listed < limit:   # each list up to the limit: the most recent overall can come from either
            page, total, more, first = api.threads(parent.id, archived=archived, offset=offset)
            totals[archived] = total
            for t in page:
                seen.setdefault(t.id, t)
            listed += len(page)
            firsts.update(first)
            if not more or not page:
                break
            offset += len(page)
    threads = sorted(seen.values(), key=lambda t: t.last_message_id or t.id, reverse=True)[:limit]
    now = archive.now()
    db.save_channels(threads, now)
    if firsts:
        db.add_strays(list(firsts.values()), now)
    label = "posts" if parent.kind == "forum" else "threads"
    lines = [f"#{parent.name or parent.id}  [{parent.id}]  · {label}, most recent activity first"]
    for t in threads:
        bits = []
        if t.message_count is not None:
            bits.append(f"{t.message_count} messages")
        bits.append(f"last message {_when(t.last_message_id or t.id)}")
        if t.kind == "private_thread":
            bits.append("private")
        if t.is_archived:
            bits.append("archived")
        lines.append(f"  {t.name}  [{t.id}]  · " + " · ".join(bits))
        if t.id in firsts and firsts[t.id].content:
            lines.append(f"      \"{_cut(firsts[t.id].content, 100)}\"")
    total_bits = [f"{what}: {totals[a]}" for a, what in ((False, "active"), (True, "archived")) if totals.get(a) is not None]
    shown = f"shown {len(threads)}"
    lines.append(f"({shown}; Discord's totals — " + ", ".join(total_bits) + ")" if total_bits else f"({shown})")
    return "\n".join(lines)
