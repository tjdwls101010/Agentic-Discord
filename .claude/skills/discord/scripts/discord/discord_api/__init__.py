"""Discord's REST API, as a user token reaches it: GET-only requests with waits and retries, snowflakes and times, permission bits, and every raw response turned into the skill's terms (`discord.records`). Nothing outside this unit reads a Discord path or field name.

`connect(token, token_hint)` gives a `Discord`; its methods are the endpoints the skill uses, plus `get` for any other path.
"""

from __future__ import annotations

from .api import PAGE, SEARCH_MAX_OFFSET, SEARCH_PAGE, THREAD_PAGE, Discord
from .http import USER_AGENT, Http, HttpFailure
from .parse import message as parse_message
from .parse import role_mentions
from .permissions import channel_access
from .snowflake import at, from_discord_time, iso, parse_bound, parse_link, time_of

__all__ = ["Discord", "Http", "HttpFailure", "PAGE", "SEARCH_MAX_OFFSET", "SEARCH_PAGE", "THREAD_PAGE", "USER_AGENT",
           "at", "connect", "from_discord_time", "iso", "parse_bound", "parse_link", "parse_message", "role_mentions",
           "time_of", "channel_access"]


def connect(token: str, *, token_hint: str, base_url: str | None = None, gap: float | None = None) -> Discord:
    """A `Discord` sending `token`. `token_hint` is what a 401 tells the user to do."""
    return Discord(Http(token, token_hint=token_hint, base_url=base_url, gap=gap))
