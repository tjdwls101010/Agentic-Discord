"""Discord's CDN, where attachment links point: reading a link's expiry, and downloading one.

This is a module function, not a method of `Http`, so the token has no way here: a link is fetched without it (measured: the CDN needs only a User-Agent). Only an https link to one of Discord's CDN hosts, with no user info and no port, is requested; a redirect is a failure, never followed. What was measured and can change without notice: links are signed and expire 12-24 hours after the message was fetched (`ex`, hex Unix seconds), an expired or altered link answers 404, and what `url` serves can differ in type and size from what the message declares.
"""

from __future__ import annotations

import http.client
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from discord.errors import Refusal

from .http import USER_AGENT, _NoRedirect
from .snowflake import iso

HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
#: For tests only: the scheme and host every CDN link is sent to instead. Only a loopback http address is accepted.
TEST_BASE = "DISCORD_CDN_BASE"
_LOOPBACK = re.compile(r"http://(?:127\.0\.0\.1|localhost):\d{1,5}")
_CHUNK = 1 << 16
_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass(frozen=True)
class Download:
    """How one download ended. `outcome`: saved (all of it was written to the file given), expired (the link no longer works: get the message again for a new one), too_large, refused (not a link to Discord's CDN; nothing was requested), failed. `content_type` and `size` are the answer's, not the message's."""
    outcome: str
    content_type: str | None = None
    size: int = 0
    detail: str = ""


def expires_at(url: str) -> str | None:
    """When a signed attachment link stops working, in the archive's time format; None for a link without `ex`."""
    try:
        ex = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("ex")
        return iso(datetime.fromtimestamp(int(ex[0], 16), tz=timezone.utc)) if ex else None
    except (ValueError, OverflowError, OSError):
        return None


def _target(url: str) -> str | None:
    """Where to send `url`: itself when it is an https link to a CDN host with no user info or port, else None. Checked before any test base replaces the host."""
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        return None
    if u.scheme != "https" or u.hostname not in HOSTS or port is not None or u.netloc.lower() != u.hostname:
        return None
    base = os.environ.get(TEST_BASE)
    if base is None:
        return url
    if not _LOOPBACK.fullmatch(base):
        raise Refusal(f"{TEST_BASE} is {base!r}; it may only be http://127.0.0.1:<port> or http://localhost:<port>", fix=f"unset {TEST_BASE}")
    return base + urllib.parse.urlunsplit(("", "", u.path, u.query, ""))


def download(url: str, out, *, max_bytes: int) -> Download:
    """GET an attachment link and write its body to `out` (a binary file). `saved` only when the whole body arrived: as many bytes as its Content-Length said, and no more than `max_bytes`. Otherwise `out` may hold part of it, and the caller discards it."""
    target = _target(url)
    if target is None:
        return Download("refused", detail="not an https link to Discord's CDN")
    req = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
    try:
        with _OPENER.open(req, timeout=60) as r:
            kind = (r.headers.get("Content-Type") or "").split(";")[0].strip() or None
            length = r.headers.get("Content-Length")
            if length is None or not length.isdigit():
                return Download("failed", kind, detail="the answer had no Content-Length, so a complete file cannot be told from a cut one")
            length = int(length)
            if length > max_bytes:
                return Download("too_large", kind, length, detail=f"{length} bytes")
            got = 0
            while chunk := r.read(min(_CHUNK, length - got + 1)):
                got += len(chunk)
                if got > max_bytes:
                    return Download("too_large", kind, got, detail=f"more than {max_bytes} bytes")
                out.write(chunk)
                if got > length:
                    break
            if got != length:
                return Download("failed", kind, got, detail=f"the answer ended at {got} of {length} bytes")
            return Download("saved", kind, got)
    except urllib.error.HTTPError as e:
        with e:
            if e.code == 404:
                return Download("expired", detail="the CDN answered 404 (an expired or no longer valid link)")
            where = f" to {e.headers.get('Location')}" if 300 <= e.code < 400 and e.headers else ""
            return Download("failed", detail=f"the CDN answered HTTP {e.code}{where}")
    except http.client.IncompleteRead as e:
        return Download("failed", detail=f"the answer ended early ({len(e.partial)} more bytes, then nothing)")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as e:
        return Download("failed", detail=f"network error ({type(e).__name__})")
