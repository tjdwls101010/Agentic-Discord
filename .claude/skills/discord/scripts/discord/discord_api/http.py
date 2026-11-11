"""GET requests to Discord's REST API, with the waits and retries a user token needs.

There is deliberately no way to send any other method: `get` takes no method argument and builds every request as a GET, so the skill stays read-only by construction.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from discord.errors import Refusal

BASE_URL = "https://discord.com/api/v9"
#: One fixed, honest User-Agent. Discord refuses requests without one (403); nothing here imitates a client.
USER_AGENT = "Mozilla/5.0 (compatible; agentic-discord-skill)"
GAP_SECONDS = 0.5
RATE_LIMIT_RETRIES = 5
INDEXING_RETRIES = 5
BACKOFF = (1, 2, 4, 8)
#: The longest single wait a 429 or 202 may ask for before it counts as a failure instead.
LONGEST_WAIT = 60.0


def _log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


class HttpFailure(Refusal):
    def __init__(self, message: str, *, status: int | None = None, code: int | None = None, fix: str | None = None,
                 kind: str = "failed") -> None:
        super().__init__(message, fix=fix, kind=kind)
        self.status = status
        self.code = code


class Http:
    """Sequential GETs: at least `gap` seconds between requests, retries as Discord asks, and failures that never look like empty results."""

    def __init__(self, token: str, *, token_hint: str, base_url: str | None = None, gap: float | None = None,
                 sleep=time.sleep, clock=time.monotonic) -> None:
        self._token = token
        self._token_hint = token_hint
        self.base_url = (base_url or os.environ.get("DISCORD_API_BASE") or BASE_URL).rstrip("/")
        env_gap = os.environ.get("DISCORD_REQUEST_GAP")
        self.gap = gap if gap is not None else (float(env_gap) if env_gap else GAP_SECONDS)
        self._sleep = sleep
        self._clock = clock
        self._last = None
        self.requests = 0

    def _wait_gap(self) -> None:
        if self._last is not None:
            left = self.gap - (self._clock() - self._last)
            if left > 0:
                self._sleep(left)

    def _once(self, url: str) -> tuple[int, dict, bytes]:
        self._wait_gap()
        req = urllib.request.Request(url, headers={"Authorization": self._token, "User-Agent": USER_AGENT, "Accept": "application/json"})
        self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers or {}), e.read()
        finally:
            self._last = self._clock()

    def get(self, path: str, query: dict | list | None = None):
        """GET `path` (relative to the API base) with `query`; return the parsed JSON body of a 200.

        Raises HttpFailure: kind `auth` for 401 (token missing or invalid), `forbidden` for 403, `not_found` for 404, `failed` for anything retries did not cure.
        """
        q = urllib.parse.urlencode(query, doseq=True) if query else ""
        url = f"{self.base_url}/{path.lstrip('/')}" + (f"?{q}" if q else "")
        shown = f"GET /{path.lstrip('/')}" + (f"?{q}" if q else "")
        limited = indexing = errors = 0
        while True:
            try:
                status, headers, body = self._once(url)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                if errors < len(BACKOFF):
                    _log(f"{shown}: network error ({type(e).__name__}); retrying in {BACKOFF[errors]}s")
                    self._sleep(BACKOFF[errors])
                    errors += 1
                    continue
                raise HttpFailure(f"{shown}: network error after {errors + 1} attempts ({type(e).__name__})") from None
            data = _json(body)
            if status == 200:
                if data is _BAD:
                    raise HttpFailure(f"{shown}: Discord answered 200 with a body that is not JSON", status=200)
                return data
            code = data.get("code") if isinstance(data, dict) else None
            if status == 429 and limited < RATE_LIMIT_RETRIES:
                wait = _retry_after(data, headers)
                if wait > LONGEST_WAIT:
                    raise HttpFailure(f"{shown}: rate limited for {wait:.0f}s; stopping rather than waiting", status=429,
                                      fix="wait and try again later; repeated limits are pressure on the account")
                _log(f"{shown}: rate limited; waiting {wait:.1f}s")
                self._sleep(wait)
                limited += 1
                continue
            if status == 202 and indexing < INDEXING_RETRIES:
                wait = min(_retry_after(data, headers) or 2.0, LONGEST_WAIT)
                _log(f"{shown}: Discord is still indexing this search; retrying in {wait:.1f}s")
                self._sleep(wait)
                indexing += 1
                continue
            if (status == 408 or status >= 500) and errors < len(BACKOFF):
                _log(f"{shown}: HTTP {status}; retrying in {BACKOFF[errors]}s")
                self._sleep(BACKOFF[errors])
                errors += 1
                continue
            message = data.get("message") if isinstance(data, dict) else None
            detail = f" ({message}, code {code})" if message else ""
            if status == 401:
                raise HttpFailure(f"Discord rejected the token{detail}", status=401, code=code, kind="auth", fix=self._token_hint)
            if status == 403:
                what = "you have no access to this" if code == 50001 else "Discord refused this for a user token"
                raise HttpFailure(f"{shown}: {what}{detail}", status=403, code=code, kind="forbidden")
            if status == 404:
                raise HttpFailure(f"{shown}: not found{detail}", status=404, code=code, kind="not_found")
            if status == 429:
                raise HttpFailure(f"{shown}: still rate limited after {limited} waits; stopping", status=429,
                                  fix="wait and try again later; repeated limits are pressure on the account")
            if status == 202:
                raise HttpFailure(f"{shown}: Discord was still indexing after {indexing} retries", status=202,
                                  fix="try the search again in a minute")
            raise HttpFailure(f"{shown}: HTTP {status}{detail}", status=status, code=code,
                              kind="bad_request" if status == 400 else "failed")


_BAD = object()


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _BAD


def _retry_after(data, headers: dict) -> float:
    if isinstance(data, dict) and isinstance(data.get("retry_after"), (int, float)):
        return float(data["retry_after"])
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                return float(v)
            except ValueError:
                pass
    return 1.0
