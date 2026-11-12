"""`doctor` through the real entry point, on a copy of the skill so each test can break the token file its own way."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.fake_discord import TOKEN, FakeDiscord
from support.harness import SCRIPTS


class DoctorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        self.scripts = self.root / "skill" / "scripts"
        shutil.copytree(SCRIPTS, self.scripts, ignore=shutil.ignore_patterns("__pycache__", ".env"))
        self.env_file = self.scripts / ".env"
        self.fake = FakeDiscord().__enter__()
        self.addCleanup(self.fake.__exit__)

    def write_env(self, text: str, mode: int = 0o600) -> None:
        self.env_file.write_text(text)
        os.chmod(self.env_file, mode)

    def doctor(self, **env: str):
        base = {k: v for k, v in os.environ.items() if not k.startswith("DISCORD_")}
        base.update({"DISCORD_API_BASE": self.fake.url, "DISCORD_SKILL_DATA": str(self.root / "data"), **env})
        r = subprocess.run([sys.executable, str(self.scripts / "cli.py"), "doctor"], capture_output=True, text=True, env=base, cwd=self.root)
        return r.returncode, json.loads(r.stdout), r.stdout + r.stderr

    def assertBlocked(self, code: int, report: dict, words: str) -> None:
        self.assertEqual(code, 3)
        self.assertFalse(report["ok"])
        self.assertTrue(any(words in b["problem"] for b in report["blockers"]), report["blockers"])
        self.assertTrue(all(b["fix"] for b in report["blockers"]))

    def test_a_good_token_file_is_ok_and_only_the_account_name_is_shown(self) -> None:
        self.write_env(f"DISCORD_TOKEN={TOKEN}\n")
        code, report, raw = self.doctor()
        self.assertEqual(code, 0, raw)
        self.assertTrue(report["ok"])
        self.assertEqual(report["account"], "Me")
        self.assertNotIn(TOKEN, raw)

    def test_no_token_file_says_where_and_how_to_write_it(self) -> None:
        code, report, raw = self.doctor()
        self.assertBlocked(code, report, "does not exist")
        fix = report["blockers"][0]["fix"]
        self.assertIn(str(self.env_file), fix)
        self.assertIn("DISCORD_TOKEN=", fix)
        self.assertIn("chmod 600", fix)
        self.assertIn("Do not paste the token into the chat", fix)

    def test_a_file_others_can_read_is_blocked_with_the_chmod(self) -> None:
        self.write_env(f"DISCORD_TOKEN={TOKEN}\n", 0o644)
        code, report, raw = self.doctor()
        self.assertBlocked(code, report, "readable by other users")
        self.assertIn(f'chmod 600 "{self.env_file}"', json.dumps(report, ensure_ascii=False).replace("\\\"", "\""))
        self.assertNotIn(TOKEN, raw)

    def test_a_file_git_would_commit_is_blocked(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.write_env(f"DISCORD_TOKEN={TOKEN}\n")
        code, report, raw = self.doctor()
        self.assertBlocked(code, report, "not ignored")
        (self.root / ".gitignore").write_text(".env\n")
        code, report, raw = self.doctor()
        self.assertEqual(code, 0, raw)

    def test_a_token_discord_rejects_is_blocked_without_showing_it(self) -> None:
        self.write_env("DISCORD_TOKEN=revoked-token-value\n")
        code, report, raw = self.doctor()
        self.assertBlocked(code, report, "rejected the token")
        self.assertNotIn("revoked-token-value", raw)

    def test_the_environment_variable_wins_over_the_file(self) -> None:
        self.write_env("DISCORD_TOKEN=revoked-token-value\n")
        code, report, raw = self.doctor(DISCORD_TOKEN=TOKEN)
        self.assertEqual(code, 0, raw)
        self.assertEqual(report["token_source"], "environment variable DISCORD_TOKEN")

    def test_a_command_without_a_token_refuses_with_the_same_fix(self) -> None:
        base = {k: v for k, v in os.environ.items() if not k.startswith("DISCORD_")}
        base.update({"DISCORD_API_BASE": self.fake.url, "DISCORD_SKILL_DATA": str(self.root / "data")})
        r = subprocess.run([sys.executable, str(self.scripts / "cli.py"), "servers"], capture_output=True, text=True, env=base)
        self.assertEqual(r.returncode, 1)
        out = json.loads(r.stdout)
        self.assertEqual(out["error"], "auth")
        self.assertIn(str(self.env_file), out["fix"])


class RejectedToken(DoctorCase):
    """A rejected token can mean Discord reset the account over suspected automation, so the fix for a 401 says to check the account first; a missing token is only a missing file."""

    STANDING = "Account Standing"

    def test_doctor_and_a_command_given_a_rejected_token_say_to_check_the_account(self) -> None:
        self.write_env("DISCORD_TOKEN=revoked-token-value\n")
        code, report, raw = self.doctor()
        fix = next(b["fix"] for b in report["blockers"] if "rejected the token" in b["problem"])
        self.assertIn(self.STANDING, fix)
        self.assertIn("password", fix)
        self.assertIn(str(self.env_file), fix)
        base = {k: v for k, v in os.environ.items() if not k.startswith("DISCORD_")}
        base.update({"DISCORD_API_BASE": self.fake.url, "DISCORD_SKILL_DATA": str(self.root / "data")})
        r = subprocess.run([sys.executable, str(self.scripts / "cli.py"), "servers"], capture_output=True, text=True, env=base)
        out = json.loads(r.stdout)
        self.assertEqual((r.returncode, out["error"]), (1, "auth"))
        self.assertIn(self.STANDING, out["fix"])

    def test_no_token_file_is_not_told_about_the_account(self) -> None:
        code, report, raw = self.doctor()
        self.assertNotIn(self.STANDING, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
