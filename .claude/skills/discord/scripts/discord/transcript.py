"""Messages whose names are already resolved -> transcript text. Pure: no requests, no database.

The notation is documented in `read --help`; a change here changes that contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from discord.records import Embed, Message, User

_MENTION = re.compile(r"<@!?(\d+)>|<#(\d+)>|<@&(\d+)>|<a?:(\w+):(\d+)>|<t:(-?\d+)(?::([tTdDfFR]))?>")
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)
_QUOTE = 60
_EVENT_PHRASES = {
    "join": "{a} joined the server",
    "call": "{a} started a call",
    "icon": "{a} changed the channel icon",
    "boost": "{a} boosted the server",
    "follow": "{a} followed an announcement channel",
    "discovery": "{a}: server discovery notice",
    "invite_reminder": "{a}: invite reminder",
    "automod": "{a}: AutoMod acted",
    "role_subscription": "{a} bought a role subscription",
    "premium_upsell": "{a}: premium notice",
    "stage_speaker": "{a} became a stage speaker",
    "app_premium": "{a}: app premium notice",
    "incident": "{a}: raid-protection notice",
    "purchase": "{a} made a purchase",
}


@dataclass(frozen=True)
class Names:
    """Display names by id. A user missing here is shown by the name the message itself carries; a channel or role missing here stays an id."""
    users: dict[int, str] = field(default_factory=dict)
    channels: dict[int, str] = field(default_factory=dict)
    roles: dict[int, str] = field(default_factory=dict)

    def of(self, u: User) -> str:
        return self.users.get(u.id) or u.global_name or u.username or f"unknown({u.id})"


def referenced_ids(messages: list[Message]) -> tuple[set[int], set[int], set[int]]:
    """(user ids, channel ids, role ids) the transcript of `messages` will need names for: people on the messages and every mention token in the text it prints."""
    users: set[int] = set()
    channels: set[int] = set()
    roles: set[int] = set()

    def scan(text: str | None) -> None:
        for m in _MENTION.finditer(_CODE.sub("", text or "")):
            if m.group(1):
                users.add(int(m.group(1)))
            elif m.group(2):
                channels.add(int(m.group(2)))
            elif m.group(3):
                roles.add(int(m.group(3)))

    def walk(msg: Message) -> None:
        users.update(u.id for u in msg.people())
        channels.add(msg.channel_id)
        scan(msg.content)
        for e in msg.embeds:
            scan(e.description)
            for _, v in e.fields:
                scan(v)
        if msg.forward:
            scan(msg.forward.content)
            if msg.forward.channel_id:
                channels.add(msg.forward.channel_id)
        if msg.referenced:
            walk(msg.referenced)

    for msg in messages:
        walk(msg)
    return users, channels, roles


def _local(iso: str | None, fmt: str = "%H:%M") -> str:
    if not iso:
        return "?"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime(fmt)


def _unix(seconds: int, style: str | None) -> str:
    t = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone()
    return t.strftime("%Y-%m-%d" if style in ("d", "D") else "%H:%M" if style in ("t", "T") else "%Y-%m-%d %H:%M")


def resolve(text: str, names: Names) -> str:
    """Mention, emoji and timestamp tokens written out; code spans and blocks left as they are."""
    def one(m: re.Match) -> str:
        if m.group(1):
            uid = int(m.group(1))
            return "@" + (names.users.get(uid) or f"unknown({uid})")
        if m.group(2):
            cid = int(m.group(2))
            return "#" + (names.channels.get(cid) or f"unknown({cid})")
        if m.group(3):
            rid = int(m.group(3))
            return "@" + (names.roles.get(rid) or f"unknown-role({rid})")
        if m.group(4):
            return f":{m.group(4)}:"
        return _unix(int(m.group(6)), m.group(7))

    parts = _CODE.split(text)
    return "".join(p if i % 2 else _MENTION.sub(one, p) for i, p in enumerate(parts))


def _quote(msg: Message, names: Names) -> str:
    text = " ".join(resolve(msg.content, names).split())
    if not text:
        text = msg.attachments[0].filename if msg.attachments else (msg.embeds[0].title if msg.embeds and msg.embeds[0].title else "")
    return text if len(text) <= _QUOTE else text[: _QUOTE - 1] + "…"


def _indent(text: str) -> str:
    return "\n".join(line if i == 0 or not line.strip() else "    " + line for i, line in enumerate(text.split("\n")))


def _embed(e: Embed, names: Names, skip_url: str) -> str:
    bits = [b for b in (e.author, e.title) if b]
    if e.description:
        bits.append(" ".join(resolve(e.description, names).split()))
    bits += [f"{k}: {' '.join(resolve(v, names).split())}" for k, v in e.fields]
    if e.footer:
        bits.append(e.footer)
    if e.url and e.url not in skip_url:
        bits.append(e.url)
    return " | ".join(bits)


def _details(msg: Message, names: Names) -> list[str]:
    out = []
    for a in msg.attachments:
        kind = f" ({a.content_type})" if a.content_type else ""
        out.append(f"  + file: {a.filename}{kind} {a.url}")
    for e in msg.embeds:
        text = _embed(e, names, msg.content)
        if text:
            out.append(f"  + embed: {text}")
    for s in msg.stickers:
        out.append(f"  + sticker: {s}")
    if msg.poll:
        p = msg.poll
        answers = " · ".join(f"{t} {v}" if v is not None else t for t, v in p.answers)
        state = "final" if p.is_final else (f"open until {_local(p.expires_at, '%Y-%m-%d %H:%M')}" if p.expires_at else "open")
        out.append(f"  + poll: \"{p.question}\" — {answers} ({state})")
    if msg.reactions:
        out.append("  + reactions: " + ", ".join(f"{r.emoji} {r.count}" for r in msg.reactions))
    if msg.thread:
        tid, name, count = msg.thread
        out.append(f"  + thread: {name} [{tid}]" + (f" · {count} messages" if count is not None else ""))
    return out


def _event_line(msg: Message, names: Names) -> str:
    ev = msg.event
    a = names.of(msg.author)
    w = ev.what if ev else ""
    if w == "pin":
        return f"* {a} pinned a message" + (f" [{ev.message_id}]" if ev.message_id else "")
    if w == "thread_created":
        return f"* {a} started a thread: {ev.name or '?'}" + (f" [{ev.thread_id}]" if ev.thread_id else "")
    if w in ("recipient_add", "recipient_remove"):
        who = names.of(ev.user) if ev.user else "someone"
        if w == "recipient_remove" and ev.user and ev.user.id == msg.author.id:
            return f"* {a} left the group"
        return f"* {a} {'added' if w == 'recipient_add' else 'removed'} {who} {'to' if w == 'recipient_add' else 'from'} the group"
    if w == "rename":
        return f"* {a} renamed the channel to {ev.name or '?'}"
    if w in ("stage_start", "stage_end", "stage_topic"):
        verb = {"stage_start": "started the stage", "stage_end": "ended the stage", "stage_topic": "changed the stage topic"}[w]
        return f"* {a} {verb}" + (f": {ev.name}" if ev.name else "")
    if w == "poll_result":
        won = f"\"{ev.winner}\" won with {ev.winner_votes} of {ev.total_votes} votes" if ev.winner else f"no winner ({ev.total_votes} votes)"
        return f"* poll \"{ev.name or '?'}\" ended: {won}"
    phrase = _EVENT_PHRASES.get(w, "{a}: " + w).format(a=a)
    text = resolve(msg.content, names).strip()
    return f"* {phrase}" + (f": {_indent(text)}" if text and w in ("follow", "automod") else "")


def _message(msg: Message, names: Names, show_channel: bool) -> list[str]:
    head = _local(msg.sent_at)
    where = f"#{names.channels.get(msg.channel_id) or msg.channel_id} · " if show_channel else ""
    tail = f"  [{msg.id}]"
    flags = [f for f, on in (("edited", msg.edited_at), ("pinned", msg.pinned)) if on]
    if msg.kind == "system":
        return [f"{head} {where}{_event_line(msg, names)}{tail}"] + _details(msg, names)
    if msg.kind == "thread_starter":
        o = msg.referenced
        if not o:
            gone = "was deleted" if msg.referenced_missing else f"is [{msg.channel_id}] in the parent channel"
            return [f"{head} {where}* thread start: the original message {gone}{tail}"]
        src = names.channels.get(o.channel_id) or str(o.channel_id)
        line = (f"{head} {where}{names.of(o.author)} (thread start; the original is [{o.id}] in #{src}, "
                f"sent {_local(o.sent_at, '%Y-%m-%d %H:%M')}): {_indent(resolve(o.content, names))}{tail}")
        return [line] + [d for d in _details(o, names) if not d.startswith("  + thread: ")]
    notes = []
    if msg.kind == "reply":
        if msg.referenced:
            notes.append(f"reply to {names.of(msg.referenced.author)}: \"{_quote(msg.referenced, names)}\"")
        elif msg.referenced_missing:
            notes.append("reply to a deleted message")
        else:
            notes.append(f"reply to [{msg.reply_to_id}]")
    if msg.command_user:
        notes.append(f"{names.of(msg.command_user)} used /{msg.command}" if msg.command else f"for {names.of(msg.command_user)}")
    notes += flags
    note = f" ({'; '.join(notes)})" if notes else ""
    body = resolve(msg.content, names)
    if msg.kind == "other":
        body = f"[type {msg.type}] {body}".rstrip()
    lines = [f"{head} {where}{names.of(msg.author)}{note}: {_indent(body)}{tail}"]
    if msg.forward:
        f = msg.forward
        src = names.channels.get(f.channel_id) if f.channel_id else None
        origin = ", ".join(x for x in (f"from #{src or f.channel_id}" if f.channel_id else None,
                                       f"message [{f.message_id}]" if f.message_id else None,
                                       f"sent {_local(f.sent_at, '%Y-%m-%d %H:%M')}" if f.sent_at else None) if x)
        lines.append(f"  + forwarded ({origin}; Discord does not say who wrote it): {_indent(resolve(f.content, names))}")
        lines += [f"  + forwarded file: {a.filename} {a.url}" for a in f.attachments]
        lines += [f"  + forwarded embed: {t}" for e in f.embeds if (t := _embed(e, names, f.content))]
    return lines + _details(msg, names)


def render(messages: list[Message], names: Names, *, show_channel: bool = False) -> str:
    """The transcript of `messages` in the order given, with a `── date ──` line whenever the local day changes."""
    out: list[str] = []
    day = None
    for msg in messages:
        d = _local(msg.sent_at, "%Y-%m-%d %a")
        if d != day:
            out.append(f"── {d} ──")
            day = d
        out += _message(msg, names, show_channel)
    return "\n".join(out)
