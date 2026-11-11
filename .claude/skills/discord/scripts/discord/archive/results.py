"""Per-call result files: one new file per call, never overwritten, so a later call cannot change what an earlier one returned."""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path


def write_result(directory: Path, command: str, text: str) -> Path:
    """Write `text` to a new file under `directory` named for the time and `command`, and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    while True:
        path = directory / f"{stamp}-{command}-{secrets.token_hex(2)}.txt"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        return path
