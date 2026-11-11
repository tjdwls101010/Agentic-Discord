"""The one way a unit says it will not or cannot do what was asked."""

from __future__ import annotations


class Refusal(Exception):
    """A request the skill will not or cannot carry out; `message` says why and `fix` what to do instead."""

    def __init__(self, message: str, *, fix: str | None = None, kind: str = "refused") -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix
        self.kind = kind
