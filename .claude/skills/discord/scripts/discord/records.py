"""The skill's own terms for what Discord returns. `discord_api` builds these from raw responses; the archive stores them and `transcript` renders them, so neither reads a Discord field name.

Every id is a snowflake as int; every time is UTC ISO-8601 text ending in Z.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Message kinds, as the archive's `messages.kind` column names them.
MESSAGE_KINDS = ("message", "reply", "thread_starter", "system", "other")

#: Channel kinds, as the archive's `channels.kind` column names them.
CHANNEL_KINDS = ("text", "announcement", "forum", "voice", "stage", "category",
                 "thread", "private_thread", "dm", "group_dm", "other", "unknown")

#: Channel kinds whose messages `read` can fetch.
READABLE_KINDS = ("text", "announcement", "voice", "stage", "thread", "private_thread", "dm", "group_dm")


@dataclass(frozen=True)
class User:
    id: int
    username: str
    global_name: str | None = None
    is_bot: bool = False


@dataclass(frozen=True)
class Attachment:
    """A file on a message as the message declares it. `url` is signed and stops working at `expires_at`; `content_type` and `size` are what the message says, which the file the url serves can differ from."""
    filename: str
    url: str
    content_type: str | None = None
    size: int | None = None
    id: int | None = None
    width: int | None = None
    height: int | None = None
    expires_at: str | None = None


@dataclass(frozen=True)
class Embed:
    url: str | None = None
    title: str | None = None
    description: str | None = None
    author: str | None = None
    fields: tuple[tuple[str, str], ...] = ()
    footer: str | None = None


@dataclass(frozen=True)
class Reaction:
    emoji: str      # a unicode emoji, `:name:` for a custom one, or `:id:` when Discord gives no name
    count: int


@dataclass(frozen=True)
class Poll:
    question: str
    answers: tuple[tuple[str, int | None], ...]   # (answer text, votes; None when Discord gave no count)
    expires_at: str | None
    is_final: bool


@dataclass(frozen=True)
class Forward:
    """A forwarded message's copy. Discord does not say who wrote the original."""
    channel_id: int | None
    message_id: int | None
    sent_at: str | None
    content: str
    attachments: tuple[Attachment, ...] = ()
    embeds: tuple[Embed, ...] = ()


@dataclass(frozen=True)
class Event:
    """What a system message records. `what` is one word; the other fields carry what the word needs (a pin's `message_id`, a new thread's `thread_id` and `name`, a group change's `user`)."""
    what: str            # join, pin, call, recipient_add, recipient_remove, rename, icon, thread_created, boost, poll_result, other
    name: str | None = None
    user: User | None = None
    thread_id: int | None = None
    message_id: int | None = None
    winner: str | None = None
    winner_votes: int | None = None
    total_votes: int | None = None


@dataclass(frozen=True)
class Message:
    id: int
    channel_id: int
    author: User
    sent_at: str
    kind: str                     # one of MESSAGE_KINDS
    type: int                     # Discord's number for the kind, kept for `[type N]`
    content: str
    raw: dict                     # the message object exactly as received
    server_id: int | None = None
    edited_at: str | None = None
    reply_to_id: int | None = None
    referenced: "Message | None" = None   # a reply's target, or a thread starter's original, when Discord sent it
    referenced_missing: bool = False      # Discord said the referenced message is deleted
    attachments: tuple[Attachment, ...] = ()
    embeds: tuple[Embed, ...] = ()
    reactions: tuple[Reaction, ...] = ()
    stickers: tuple[str, ...] = ()
    pinned: bool = False
    mentions: tuple[User, ...] = ()
    thread: tuple[int, str, int | None] | None = None   # (id, name, message count) of a thread started from this message
    forward: Forward | None = None
    command_user: User | None = None             # who ran the app command this message answers
    command: str | None = None
    poll: Poll | None = None
    event: Event | None = None

    def people(self) -> list[User]:
        """Everyone this message names: author, mentions, command user, event user, and the same for what it references."""
        out = [self.author, *self.mentions]
        out += [u for u in (self.command_user, self.event.user if self.event else None) if u]
        if self.referenced:
            out += self.referenced.people()
        return out


@dataclass(frozen=True)
class Channel:
    id: int
    kind: str                     # one of CHANNEL_KINDS
    name: str | None
    server_id: int | None = None
    parent_id: int | None = None
    topic: str | None = None
    position: int = 0
    is_archived: bool = False
    last_message_id: int | None = None
    can_view: bool | None = None
    can_read_history: bool | None = None
    message_count: int | None = None
    recipients: tuple[User, ...] = ()


@dataclass(frozen=True)
class Server:
    id: int
    name: str
    member_count: int | None = None
    roles: dict[int, str] = field(default_factory=dict)
