"""Per-call result files: one new file per call, never overwritten, so a later call cannot change what an earlier one returned."""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path


def write_result(root: Path, command: str, text: str) -> Path:
    """Write `text` to a new file under `root`/results named for the time and `command`, and return its path. `root` gets a .gitignore of `*` the first time, so results never reach a commit of the project they were made in."""
    directory = root / "results"
    directory.mkdir(parents=True, exist_ok=True)
    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
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
