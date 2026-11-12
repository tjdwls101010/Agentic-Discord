"""Scaffolding shared by the tests: where the skill is, importing a unit's interface, and running the real CLI.

`DISCORD_SKILL_SCRIPTS` points the whole suite at another copy of `scripts/`, which is how a deliberately broken copy is shown to turn tests red without touching the real one.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO / ".claude" / "skills" / "discord"
SCRIPTS = Path(os.environ.get("DISCORD_SKILL_SCRIPTS") or SKILL_DIR / "scripts")
ENTRY = SCRIPTS / "cli.py"
SUPPORT = Path(__file__).resolve().parent
FIXTURES = SUPPORT / "fixtures"


def engine(module: str):
    """Import a unit's interface (`"discord.archive"`, `"discord.transcript"`, …) from the scripts under test. Only interfaces: tests/test_structure.py refuses a module inside a unit."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(module)


def run_cli(*args: str, env: dict | None = None, cwd: str | None = None, timeout: float = 120, stdin: str | None = None,
            scripts: Path | None = None):
    """Run the real entry point as the model would, in a clean environment unless `env` adds to it. Returns the CompletedProcess.

    The clean environment drops only `DISCORD_*` variables, so the real `scripts/.env` is still read when no token is given: a test that must run without a token passes `scripts=tokenless_copy()`.
    """
    base = {k: v for k, v in os.environ.items() if not k.startswith("DISCORD_")}
    base.update(env or {})
    entry = (scripts / "cli.py") if scripts else ENTRY
    return subprocess.run([sys.executable, str(entry), *args], capture_output=True, text=True, env=base,
                          cwd=cwd or tempfile.mkdtemp(), timeout=timeout, input=stdin)


def tokenless_copy() -> Path:
    """A copy of `scripts/` without `.env`, in a new temporary directory, for runs that must not find the real token."""
    copy = Path(tempfile.mkdtemp()) / "scripts"
    shutil.copytree(SCRIPTS, copy, ignore=shutil.ignore_patterns("__pycache__", ".env"))
    return copy


def closed_port_url() -> str:
    """An API base on a loopback port nobody listens on: a request sent there fails at once and never leaves the machine."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/api/v9"


def last_json(text: str) -> dict:
    """The last line of `text` read as JSON (refusals and `doctor` print one JSON line)."""
    return json.loads(text.strip().splitlines()[-1])
