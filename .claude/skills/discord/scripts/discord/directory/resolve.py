"""Turning what the user typed (an id, a link, a name) into a server, channel or person, and naming the people a transcript shows. A name that matches several is refused with the candidates, never guessed."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone

from discord import archive, discord_api, transcript
from discord.errors import Refusal
from discord.records import Channel, Message

#: How long a looked-up nickname is trusted before it is asked again.
MEMBER_FRESH = timedelta(days=7)


def _candidates(rows: list[str]) -> str:
    return "; ".join(rows[:15]) + (f"; and {len(rows) - 15} more" if len(rows) > 15 else "")


def resolve_server(api, db, arg: str) -> tuple[int, str]:
    """(server id, name) for an id, a link or a name of a server you are in."""
    link = discord_api.parse_link(arg)
    if link:
        if link[0] == "@me":
            raise Refusal(f"{arg!r} is a DM link, not a server", fix="give a server, or the DM's channel id to commands that take a channel", kind="usage")
        sid = int(link[0])
        return sid, db.server_name(sid) or str(sid)
    if arg.isdigit():
        return int(arg), db.server_name(int(arg)) or arg
    found = db.find_servers(arg)
    if not found:
        now = archive.now()
        for s in api.servers():
            db.save_server(s, now)
        found = db.find_servers(arg)
    if len(found) == 1:
        return found[0]
    if not found:
        raise Refusal(f"you are in no server named like {arg!r}", fix="run `servers` for the list, then give an id or an exact name")
    raise Refusal(f"{arg!r} matches {len(found)} servers: " + _candidates([f"{n} #{i}" for i, n in found]), fix="give the id or the exact name")


def _as_channel(row: dict) -> Channel:
    return Channel(id=row["id"], kind=row["kind"], name=row["name"], server_id=row["server_id"], parent_id=row["parent_id"],
                   last_message_id=row["last_message_id"],
                   can_view=None if row["can_view"] is None else bool(row["can_view"]),
                   can_read_history=None if row["can_read_history"] is None else bool(row["can_read_history"]))


def _fetch_channel(api, db, cid: int) -> Channel:
    row = db.channel(cid)
    if row and row["kind"] != "unknown":
        return _as_channel(row)
    c = api.channel(cid)
    if c.server_id and not db.server_name(c.server_id):
        now = archive.now()
        for s in api.servers():
            db.save_server(s, now)
    db.save_channels([c], archive.now())
    return _as_channel(db.channel(cid))


def resolve_channel(api, db, arg: str) -> Channel:
    """A channel, thread or DM from an id, a link, `server/channel`, or a name among channels already listed."""
    link = discord_api.parse_link(arg)
    if link and link[1]:
        return _fetch_channel(api, db, link[1])
    if arg.isdigit():
        return _fetch_channel(api, db, int(arg))
    server_id = None
    name = arg
    if "/" in arg:
        server_part, name = arg.rsplit("/", 1)
        server_id, _ = resolve_server(api, db, server_part)
        found = db.find_channels(name, server_id)
        if not found:
            now = archive.now()
            server, channels = api.server_channels(server_id)
            db.save_server(server, now)
            db.save_channels(channels, now)
            found = db.find_channels(name, server_id)
    else:
        found = db.find_channels(name)
    if len(found) == 1:
        return _as_channel(found[0])
    if not found:
        raise Refusal(f"no listed channel is named like {arg!r}",
                      fix="give an id or link, or server/channel, or run `channels <server>` (or `servers` for DMs, `threads <channel>` for threads) so the name is known")
    raise Refusal(f"{arg!r} matches {len(found)} channels: " + _candidates(
        [f"#{r['name']} ({r['kind']}{', ' + r['server'] if r['server'] else ''}) #{r['id']}" for r in found]),
        fix="give the id, or server/channel")


def resolve_target(api, db, arg: str) -> tuple[str, int, str]:
    """('server', id, name) or ('dm', channel id, name) for `search`: a server, or a DM or group DM."""
    link = discord_api.parse_link(arg)
    if link and link[0] == "@me" and link[1]:
        c = _fetch_channel(api, db, link[1])
        return "dm", c.id, c.name or str(c.id)
    if arg.isdigit():
        row = db.channel(int(arg))
        if row and row["kind"] in ("dm", "group_dm"):
            return "dm", row["id"], row["name"] or arg
    try:
        sid, name = resolve_server(api, db, arg)
        return "server", sid, name
    except Refusal:
        if arg.isdigit() or (link and link[0] != "@me"):
            raise
        dms = db.find_channels(arg, dm=True)
        if len(dms) == 1:
            return "dm", dms[0]["id"], dms[0]["name"]
        raise


_MENTION = re.compile(r"^<@!?(\d+)>$")


def resolve_user(db, server_id: int | None, arg: str) -> int:
    """A user id from an id, `<@id>`, or a name matching exactly one person the archive has seen."""
    m = _MENTION.match(arg.strip())
    if m:
        return int(m.group(1))
    if arg.isdigit():
        return int(arg)
    found = db.find_people(arg, server_id)
    if len(found) == 1:
        return found[0][0]
    if not found:
        raise Refusal(f"nobody named like {arg!r} appears in anything fetched so far",
                      fix="Discord cannot look people up by name: read or search a channel where they post, then use the name or the id shown")
    raise Refusal(f"{arg!r} matches {len(found)} people: " + _candidates([f"{n} (id {i})" for i, n in found]), fix="give the id")


def name_channels(api, db, server_id: int | None, channel_ids: set[int]) -> None:
    """Make sure the archive knows the names of `channel_ids` (search hits can come from channels never listed): the server's channel list once, then each thread still unnamed."""
    unnamed = {c for c in channel_ids if not ((row := db.channel(c)) and row["name"])}
    if unnamed and server_id:
        now = archive.now()
        server, channels = api.server_channels(server_id)
        db.save_server(server, now)
        db.save_channels(channels, now)
        unnamed = {c for c in unnamed if not ((row := db.channel(c)) and row["name"])}
    for cid in sorted(unnamed):
        try:
            db.save_channels([api.channel(cid)], archive.now())
        except discord_api.HttpFailure:
            pass


def names_for(api, db, server_id: int | None, messages: list[Message], *, look_up: bool) -> transcript.Names:
    """Names for everyone and everything `messages` will show. With `look_up`, people without a fresh server nickname are asked for one, one request each."""
    users, channels, roles = transcript.referenced_ids(messages)
    if look_up and server_id:
        fresh_after = discord_api.iso(datetime.now(timezone.utc) - MEMBER_FRESH)
        stale = sorted(db.members_to_look_up(server_id, users, fresh_after))
        if stale:
            print(f"looking up {len(stale)} server nickname(s)…", file=sys.stderr, flush=True)
            found = []
            for uid in stale:
                r = api.member(server_id, uid)
                if r is not None:
                    found.append((uid, *r))
            db.save_members(server_id, found, archive.now())
    u, c, r = db.names(server_id, users, channels, roles)
    return transcript.Names(users=u, channels=c, roles=r)
