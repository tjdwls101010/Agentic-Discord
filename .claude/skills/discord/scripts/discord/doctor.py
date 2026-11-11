"""Whether the skill can work: the token file is safe and holds a token Discord accepts, and the archive is one this version writes."""

from __future__ import annotations

from pathlib import Path

from discord import archive, credentials, discord_api
from discord.errors import Refusal


def _size(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    files = [p for p in path.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def check(data_dir: Path, output_dir: Path, *, env_file: Path = credentials.ENV_FILE) -> dict:
    """`ok` is true when nothing found would stop a command; each blocker carries its fix. Makes one request (`users/@me`) when there is a token to test."""
    blockers: list[dict] = []
    warnings: list[dict] = []
    report: dict = {"ok": False, "blockers": blockers, "warnings": warnings, "token_file": str(env_file)}
    for problem, fix in credentials.problems(env_file):
        blockers.append({"problem": problem, "fix": fix})
    token = None
    try:
        token, source = credentials.load(env_file)
        report["token_source"] = source
    except Refusal:
        pass
    if token:
        try:
            me = discord_api.connect(token, token_hint=credentials.hint(env_file)).me()
            report["account"] = me.global_name or me.username
        except discord_api.HttpFailure as e:
            blockers.append({"problem": e.message, "fix": e.fix or "check the network and run doctor again"})
    db = data_dir / "discord.db"
    report["archive"] = {"path": str(db), "bytes": 0}
    if db.exists():
        try:
            a = archive.Archive(db)
            report["archive"]["bytes"] = a.size()
            a.close()
        except Refusal as e:
            blockers.append({"problem": e.message, "fix": e.fix})
    results = output_dir / "results"
    files, size = _size(results)
    report["results"] = {"path": str(results), "files": files, "bytes": size}
    if size > 500 * 1024 * 1024:
        warnings.append({"problem": f"result files take {size // (1024 * 1024)} MB",
                         "fix": f"delete old files under {results}; each was only that call's output"})
    report["ok"] = not blockers
    return report
