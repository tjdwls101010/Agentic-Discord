"""Per-call result files: one new file per call, never overwritten, so a later call cannot change what an earlier one returned."""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path


def prepare_output(root: Path) -> None:
    """Create `root`, the working directory's output folder, with a .gitignore of `*` the first time, so nothing written under it (results, downloaded files) reaches a commit of the project it was made in."""
    root.mkdir(parents=True, exist_ok=True)
    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")


def write_result(root: Path, command: str, text: str) -> Path:
    """Write `text` to a new file under `root`/results named for the time and `command`, and return its path."""
    prepare_output(root)
    directory = root / "results"
    directory.mkdir(exist_ok=True)
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
