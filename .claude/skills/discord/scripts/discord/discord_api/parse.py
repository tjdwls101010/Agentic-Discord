"""Discord's raw JSON in the skill's own terms. Every Discord field name the skill reads is read here."""

from __future__ import annotations

from discord.records import Attachment, Channel, Embed, Event, Forward, Message, Poll, Reaction, Server, User

from .snowflake import from_discord_time

#: Message types that are someone's post. 20 and 23 are an app's answer to a slash or context-menu command.
_POSTS = {0, 20, 23}
#: System message types this version knows, by the word the transcript uses for them.
_EVENTS = {1: "recipient_add", 2: "recipient_remove", 3: "call", 4: "rename", 5: "icon", 6: "pin", 7: "join",
           8: "boost", 9: "boost", 10: "boost", 11: "boost", 12: "follow", 14: "discovery", 15: "discovery",
           16: "discovery", 17: "discovery", 18: "thread_created", 22: "invite_reminder", 24: "automod",
           25: "role_subscription", 26: "premium_upsell", 27: "stage_start", 28: "stage_end", 29: "stage_speaker",
           31: "stage_topic", 32: "app_premium", 36: "incident", 37: "incident", 38: "incident", 39: "incident",
           44: "purchase", 46: "poll_result"}
_CHANNEL_KINDS = {0: "text", 1: "dm", 2: "voice", 3: "group_dm", 4: "category", 5: "announcement", 10: "thread",
                  11: "thread", 12: "private_thread", 13: "stage", 15: "forum"}


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def user(raw: dict, *, webhook: bool = False) -> User:
    return User(id=int(raw["id"]), username=raw.get("username") or "", global_name=raw.get("global_name"),
                is_bot=bool(raw.get("bot")) or webhook)


def _attachments(raw_list) -> tuple[Attachment, ...]:
    return tuple(Attachment(filename=a.get("filename") or "", url=a.get("url") or "", content_type=a.get("content_type"),
                            size=a.get("size")) for a in raw_list or ())


def _embeds(raw_list) -> tuple[Embed, ...]:
    out = []
    for e in raw_list or ():
        out.append(Embed(url=e.get("url"), title=e.get("title"), description=e.get("description"),
                         author=(e.get("author") or {}).get("name"),
                         fields=tuple((f.get("name") or "", f.get("value") or "") for f in e.get("fields") or ()),
                         footer=(e.get("footer") or {}).get("text")))
    return tuple(out)


def _emoji(e: dict) -> str:
    if e.get("id"):
        return f":{e['name']}:" if e.get("name") else f":{e['id']}:"
    return e.get("name") or "?"


def _poll(p: dict | None) -> Poll | None:
    if not p:
        return None
    results = p.get("results") or {}
    counts = {c.get("id"): c.get("count") for c in results.get("answer_counts") or ()}
    answers = []
    for a in p.get("answers") or ():
        media = a.get("poll_media") or {}
        text = media.get("text") or (_emoji(media["emoji"]) if media.get("emoji") else "")
        answers.append((text, counts.get(a.get("answer_id"), 0 if results else None)))
    return Poll(question=(p.get("question") or {}).get("text") or "", answers=tuple(answers),
                expires_at=from_discord_time(p.get("expiry")), is_final=bool(results.get("is_finalized")))


def _event(t: int, raw: dict) -> Event:
    what = _EVENTS[t]
    mentions = raw.get("mentions") or []
    ref = raw.get("message_reference") or {}
    if what in ("recipient_add", "recipient_remove"):
        return Event(what, user=user(mentions[0]) if mentions else None)
    if what in ("rename", "stage_start", "stage_end", "stage_topic"):
        return Event(what, name=raw.get("content") or None)
    if what == "thread_created":
        return Event(what, name=raw.get("content") or None, thread_id=_int(ref.get("channel_id")))
    if what == "pin":
        return Event(what, message_id=_int(ref.get("message_id")))
    if what == "poll_result":
        fields = {}
        for e in raw.get("embeds") or ():
            for f in e.get("fields") or ():
                fields[f.get("name")] = f.get("value")
        return Event(what, name=fields.get("poll_question_text"), winner=fields.get("victor_answer_text"),
                     winner_votes=_int(fields.get("victor_answer_votes")), total_votes=_int(fields.get("total_votes")))
    return Event(what)


def message(raw: dict, *, channel_id: int | None = None, server_id: int | None = None) -> Message:
    """A message object from any endpoint (listing, search, a reply's `referenced_message`) as a Message."""
    t = int(raw.get("type", 0))
    ref = raw.get("message_reference") or {}
    if t in _POSTS:
        kind = "message"
    elif t == 19:
        kind = "reply"
    elif t == 21:
        kind = "thread_starter"
    elif t in _EVENTS:
        kind = "system"
    else:
        kind = "other"
    referenced = None
    missing = False
    if t in (19, 21):
        if raw.get("referenced_message"):
            referenced = message(raw["referenced_message"], server_id=_int(ref.get("guild_id")) or server_id)
        else:
            # Present but null: Discord says the message was deleted. Absent: it was not sent along (search hits).
            missing = "referenced_message" in raw
    forward = None
    snapshots = raw.get("message_snapshots") or []
    if ref.get("type") == 1 and snapshots:
        snap = snapshots[0].get("message") or {}
        forward = Forward(channel_id=_int(ref.get("channel_id")), message_id=_int(ref.get("message_id")),
                          sent_at=from_discord_time(snap.get("timestamp")), content=snap.get("content") or "",
                          attachments=_attachments(snap.get("attachments")), embeds=_embeds(snap.get("embeds")))
    meta = raw.get("interaction_metadata") or {}
    legacy = raw.get("interaction") or {}
    who = meta.get("user") or legacy.get("user")
    thread = raw.get("thread")
    sid = _int(raw.get("guild_id")) or server_id
    return Message(
        id=int(raw["id"]), channel_id=_int(raw.get("channel_id")) or channel_id or 0, server_id=sid,
        author=user(raw["author"], webhook=bool(raw.get("webhook_id"))),
        sent_at=from_discord_time(raw.get("timestamp")) or "", edited_at=from_discord_time(raw.get("edited_timestamp")),
        kind=kind, type=t, content=raw.get("content") or "", raw=raw,
        reply_to_id=_int(ref.get("message_id")) if t == 19 else None,
        referenced=referenced, referenced_missing=missing,
        attachments=_attachments(raw.get("attachments")), embeds=_embeds(raw.get("embeds")) if t != 46 else (),
        reactions=tuple(Reaction(_emoji(r.get("emoji") or {}), int(r.get("count") or 0)) for r in raw.get("reactions") or ()),
        stickers=tuple(s.get("name") or "" for s in raw.get("sticker_items") or ()),
        pinned=bool(raw.get("pinned")),
        mentions=tuple(user(u) for u in raw.get("mentions") or ()),
        thread=(int(thread["id"]), thread.get("name") or "", thread.get("message_count")) if thread else None,
        forward=forward,
        command_user=user(who) if who else None,
        command=meta.get("name") or legacy.get("name"),
        poll=_poll(raw.get("poll")),
        event=_event(t, raw) if kind == "system" else None,
    )


def role_mentions(raw: dict) -> list[int]:
    return [int(r) for r in raw.get("mention_roles") or ()]


def channel(raw: dict, *, server_id: int | None = None, me: int | None = None) -> Channel:
    t = int(raw.get("type", -1))
    kind = _CHANNEL_KINDS.get(t, "other")
    meta = raw.get("thread_metadata") or {}
    recipients = tuple(user(u) for u in raw.get("recipients") or ())
    name = raw.get("name")
    if kind in ("dm", "group_dm") and not name:
        name = ", ".join(u.global_name or u.username for u in recipients if u.id != me) or None
    return Channel(id=int(raw["id"]), kind=kind, name=name, server_id=_int(raw.get("guild_id")) or server_id,
                   parent_id=_int(raw.get("parent_id")), topic=raw.get("topic"), position=int(raw.get("position") or 0),
                   is_archived=bool(meta.get("archived")), last_message_id=_int(raw.get("last_message_id")),
                   message_count=raw.get("message_count"), recipients=recipients)


def server(raw: dict) -> Server:
    return Server(id=int(raw["id"]), name=raw.get("name") or "",
                  member_count=raw.get("approximate_member_count") or raw.get("member_count"),
                  roles={int(r["id"]): r.get("name") or "" for r in raw.get("roles") or ()})
