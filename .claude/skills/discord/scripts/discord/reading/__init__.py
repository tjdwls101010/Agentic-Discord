"""Reading messages: the collector that walks a channel into the archive (`collect`, with `Spec` and `Outcome`), and the `read`, `search` and `get` commands built on it."""

from __future__ import annotations

from .collect import Outcome, Spec, collect
from .commands import get, read, search

__all__ = ["Outcome", "Spec", "collect", "get", "read", "search"]
