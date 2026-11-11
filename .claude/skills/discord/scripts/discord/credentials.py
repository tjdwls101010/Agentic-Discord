"""The token: read from the `DISCORD_TOKEN` environment variable, else from `scripts/.env`, which the user writes by hand. Nothing here writes the file or shows the token."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from discord.errors import Refusal

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def hint(path: Path = ENV_FILE) -> str:
    """What to do when there is no usable token. It says to write the file, never to paste the token into the conversation."""
    return (f"write the line DISCORD_TOKEN=<your token> into {path} yourself and run: chmod 600 \"{path}\". "
            "Do not paste the token into the chat: the conversation history would keep it.")


def problems(path: Path = ENV_FILE) -> list[tuple[str, str]]:
    """(problem, fix) for each reason the token file is unsafe or unusable; empty when it is fine. The environment variable, when set, makes a missing file no problem."""
    out = []
    if not path.exists():
        if not os.environ.get("DISCORD_TOKEN"):
            out.append((f"no token: {path} does not exist and DISCORD_TOKEN is not set", hint(path)))
        return out
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        out.append((f"{path} is readable by other users (mode {mode:o})", f"chmod 600 \"{path}\""))
    if _git_would_take(path):
        out.append((f"{path} is inside a git repository and not ignored, so a commit could publish the token",
                    f"add .env to the .gitignore of the repository holding {path.parent}"))
    if not os.environ.get("DISCORD_TOKEN") and not _from_file(path):
        out.append((f"{path} has no DISCORD_TOKEN= line with a value", hint(path)))
    return out


def load(path: Path = ENV_FILE) -> tuple[str, str]:
    """(token, where it came from). Refuses, with the fix, when there is none or the file is unsafe."""
    env = os.environ.get("DISCORD_TOKEN")
    if env:
        return env.strip(), "environment variable DISCORD_TOKEN"
    found = problems(path)
    if found:
        problem, fix = found[0]
        raise Refusal(problem, fix=fix, kind="auth")
    return _from_file(path) or "", str(path)


def _from_file(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key.strip().removeprefix("export ").strip() == "DISCORD_TOKEN":
            value = value.strip().strip('"').strip("'")
            if value:
                return value
    return None


def _git_would_take(path: Path) -> bool:
    try:
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=path.parent, capture_output=True, text=True, timeout=10)
        if inside.returncode != 0:
            return False
        ignored = subprocess.run(["git", "check-ignore", "-q", path.name], cwd=path.parent, capture_output=True, timeout=10)
        return ignored.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        return False
