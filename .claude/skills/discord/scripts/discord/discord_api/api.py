"""Discord's endpoints, each returning the skill's own terms. Paths, query names and response shapes live here and nowhere else."""

from __future__ import annotations

from dataclasses import replace

from discord.records import Channel, Message, Server, User

from . import parse
from .http import Http, HttpFailure
from .permissions import channel_access

#: The most messages one listing returns; asking for more is a 400.
PAGE = 100
#: The most hits one search returns, and the deepest offset it accepts.
SEARCH_PAGE = 25
SEARCH_MAX_OFFSET = 9975
THREAD_PAGE = 25


class Discord:
    """The API as the skill uses it. Every call is a GET through `Http`."""

    def __init__(self, http: Http) -> None:
        self.http = http
        self._me: User | None = None

    @property
    def requests(self) -> int:
        return self.http.requests

    def me(self) -> User:
        if self._me is None:
            self._me = parse.user(self.http.get("users/@me"))
        return self._me

    def servers(self) -> list[Server]:
        return [parse.server(g) for g in self.http.get("users/@me/guilds", {"with_counts": "true"})]

    def dms(self) -> list[Channel]:
        me = self.me().id
        return [parse.channel(c, me=me) for c in self.http.get("users/@me/channels")]

    def server_channels(self, server_id: int) -> tuple[Server, list[Channel]]:
        """The server (with its roles) and every channel Discord lists for it, with `can_view` and `can_read_history` worked out from your roles. Discord lists channels you cannot see too."""
        guild = self.http.get(f"guilds/{server_id}", {"with_counts": "true"})
        mine = self.http.get(f"users/@me/guilds/{server_id}/member")
        me = self.me().id
        perms = {int(r["id"]): int(r.get("permissions") or 0) for r in guild.get("roles") or ()}
        roles = [int(r) for r in mine.get("roles") or ()]
        out = []
        for raw in self.http.get(f"guilds/{server_id}/channels"):
            c = parse.channel(raw, server_id=server_id)
            view, history = channel_access(server_id=server_id, owner_id=int(guild.get("owner_id") or 0), my_id=me,
                                           my_roles=roles, role_permissions=perms, overwrites=raw.get("permission_overwrites") or [])
            out.append(replace(c, can_view=view, can_read_history=history))
        return parse.server(guild), out

    def channel(self, channel_id: int) -> Channel:
        return parse.channel(self.http.get(f"channels/{channel_id}"), me=self.me().id)

    def messages(self, channel_id: int, *, limit: int = PAGE, after: int | None = None, before: int | None = None,
                 around: int | None = None) -> tuple[list[Message], set[int]]:
        """One page of a channel's messages, oldest first, and the ids Discord returned. At most one of after/before/around: Discord silently ignores `before` when `after` is also sent."""
        if sum(x is not None for x in (after, before, around)) > 1:
            raise ValueError("send at most one of after, before, around")
        q = {"limit": str(limit)}
        for k, v in (("after", after), ("before", before), ("around", around)):
            if v is not None:
                q[k] = str(v)
        data = self.http.get(f"channels/{channel_id}/messages", q)
        if not isinstance(data, list):
            raise HttpFailure(f"GET /channels/{channel_id}/messages answered 200 without a list of messages", status=200)
        msgs = sorted((parse.message(m, channel_id=channel_id) for m in data), key=lambda m: m.id)
        return msgs, {m.id for m in msgs}

    def threads(self, channel_id: int, *, archived: bool, offset: int = 0) -> tuple[list[Channel], int | None, bool, dict[int, Message]]:
        """One page of a channel's or forum's threads, last message first: (threads, total, has_more, a forum post's first message by thread id)."""
        data = self.http.get(f"channels/{channel_id}/threads/search",
                             {"archived": "true" if archived else "false", "sort_by": "last_message_time", "sort_order": "desc",
                              "limit": str(THREAD_PAGE), "offset": str(offset)})
        if not isinstance(data, dict) or not isinstance(data.get("threads"), list):
            raise HttpFailure(f"GET /channels/{channel_id}/threads/search answered 200 without a list of threads", status=200)
        threads = [parse.channel(t) for t in data["threads"]]
        firsts = {int(m["channel_id"]): parse.message(m) for m in data.get("first_messages") or ()}
        return threads, data.get("total_results"), bool(data.get("has_more")), firsts

    def member(self, server_id: int, user_id: int) -> tuple[User | None, str | None, bool] | None:
        """(user, server nickname, is a member) for one person. When Discord says they are not in the server (left, or a webhook): (None, None, False). None when the lookup itself failed."""
        try:
            raw = self.http.get(f"guilds/{server_id}/members/{user_id}")
        except HttpFailure as e:
            if e.status == 404 and e.code in (10007, 10013):
                return None, None, False
            if e.status in (403, 404):
                return None
            raise
        return parse.user(raw["user"]), raw.get("nick"), True

    def search(self, *, server_id: int | None = None, channel_id: int | None = None, query: list[tuple[str, str]]) -> tuple[int | None, list[Message]]:
        """One page of Discord's own search over a server (`server_id`) or a DM (`channel_id`): (estimated total, hits)."""
        path = f"guilds/{server_id}/messages/search" if server_id else f"channels/{channel_id}/messages/search"
        data = self.http.get(path, query)
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise HttpFailure(f"GET /{path} answered 200 without a list of hits", status=200)
        hits = []
        for group in data["messages"]:
            pick = next((m for m in group if m.get("hit")), group[0] if group else None)
            if pick:
                hits.append(parse.message(pick, server_id=server_id))
        return data.get("total_results"), hits

    def get(self, path: str, query: list[tuple[str, str]] | None = None):
        """Any API path, raw JSON back."""
        return self.http.get(path, query)
