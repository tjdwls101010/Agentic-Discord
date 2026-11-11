"""Scaffolding shared by the tests: where the skill is, importing a unit's interface, and running the real CLI.

`DISCORD_SKILL_SCRIPTS` points the whole suite at another copy of `scripts/`, which is how a deliberately broken copy is shown to turn tests red without touching the real one.
"""

from __future__ import annotations

import importlib
import json
import os
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


def run_cli(*args: str, env: dict | None = None, cwd: str | None = None, timeout: float = 120, stdin: str | None = None):
    """Run the real entry point as the model would, in a clean environment unless `env` adds to it. Returns the CompletedProcess."""
    base = {k: v for k, v in os.environ.items() if not k.startswith("DISCORD_")}
    base.update(env or {})
    return subprocess.run([sys.executable, str(ENTRY), *args], capture_output=True, text=True, env=base,
                          cwd=cwd or tempfile.gettempdir(), timeout=timeout, input=stdin)


def last_json(text: str) -> dict:
    """The last line of `text` read as JSON (refusals and `doctor` print one JSON line)."""
    return json.loads(text.strip().splitlines()[-1])
