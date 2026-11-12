"""Reading messages: the collector that walks a channel into the archive (`collect`, with `Spec` and `Outcome`), the `read`, `search` and `get` commands built on it, and `show`, which prints what the archive holds without a request."""

from __future__ import annotations

from .collect import Outcome, Spec, collect
from .commands import get, read, search, show

__all__ = ["Outcome", "Spec", "collect", "get", "read", "search", "show"]
