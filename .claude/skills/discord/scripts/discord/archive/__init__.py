"""The local archive: a SQLite database the model queries read-only, whose schema (`schema.sql`, comments included) is its interface, and the per-call result files.

Writes go through `Archive`, which keeps messages and `coverage` consistent: a page's messages, the range it proves, absences inside that range and the head mark commit together, so an interrupted collection leaves only true ranges.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .coverage import Page, Proof, prove
from .results import prepare_output, write_result
from .store import SCHEMA, SCHEMA_VERSION, Archive, Range, Stored

__all__ = ["Archive", "Page", "Proof", "Range", "SCHEMA", "SCHEMA_VERSION", "Stored", "now", "prepare_output", "prove", "write_result"]


def now() -> str:
    """The current UTC time in the archive's *_at format (microseconds, so two fetches in one run order)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
