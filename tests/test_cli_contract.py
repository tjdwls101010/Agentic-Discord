"""The command line as the model meets it: help that explains every argument and is printed as written, closed sets as choices, exit codes, a run from another directory through uv, and no token anywhere in what it prints."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support.fake_discord import TOKEN, FakeDiscord, ToyWorld
from support.harness import ENTRY, engine, run_cli

cli = engine("cli")
COMMANDS = ("doctor", "servers", "channels", "threads", "read", "search", "get", "show")


def subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return dict(action.choices)


class Help(unittest.TestCase):
    def test_every_command_and_argument_explains_itself(self) -> None:
        subs = subparsers(cli.build_parser())
        self.assertEqual(set(subs), set(COMMANDS))
        for name, p in subs.items():
            with self.subTest(command=name):
                self.assertTrue(p.description and p.epilog, name)
                self.assertIn("Exit", p.epilog)
                for a in p._actions:
                    if not isinstance(a, argparse._HelpAction):
                        self.assertTrue(a.help, f"{name} {a.dest} has no help")

    def test_closed_sets_are_choices(self) -> None:
        search = subparsers(cli.build_parser())["search"]
        by_dest = {a.dest: a for a in search._actions}
        self.assertEqual(set(by_dest["sort"].choices), {"newest", "oldest", "relevance"})
        self.assertIn("snapshot", by_dest["has"].choices)

    def test_help_paragraphs_are_printed_as_written_one_line_each(self) -> None:
        """Whatever reads the help wraps it; a paragraph broken at 80 columns loses what each piece is about."""
        for name in ("read", "search"):
            out = run_cli(name, "--help").stdout
            body = out.split("\n\n")
            longest = max(len(line) for line in out.splitlines())
            self.assertGreater(longest, 300, f"{name} --help looks re-wrapped")
            self.assertTrue(any(p.startswith("Transcript: ") for p in body) or name != "read")

    def test_the_top_level_help_spells_the_pre_approved_sqlite3_call(self) -> None:
        out = run_cli("--help").stdout
        self.assertRegex(out, r'sqlite3 -readonly ".+/data/discord\.db" "<SQL or \.schema>"')


class Exits(unittest.TestCase):
    def test_bad_arguments_exit_2_with_one_json_line_naming_the_help(self) -> None:
        r = run_cli("read")
        self.assertEqual(r.returncode, 2)
        out = json.loads(r.stdout)
        self.assertEqual((out["error"], out["help"]), ("usage", "cli.py read --help"))
        r = run_cli("search", "x", "--sort", "loudest")
        self.assertEqual(r.returncode, 2)


class FromElsewhere(unittest.TestCase):
    def test_uv_runs_it_from_another_directory_on_the_python_its_header_asks_for(self) -> None:
        if not shutil.which("uv"):
            self.skipTest("uv is not installed")
        where = tempfile.mkdtemp()
        r = subprocess.run(["uv", "run", str(ENTRY), "--version"], capture_output=True, text=True, cwd=where, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), cli.__version__)


class NoTokenInOutput(unittest.TestCase):
    def test_no_command_prints_the_token_or_writes_it_to_a_result(self) -> None:
        w = ToyWorld()
        a = w.user("ana")
        g = w.guild("Lab")
        ch = w.channel("general", guild=g)
        w.post(ch, a, f"token-looking text {TOKEN[:5]}")
        with FakeDiscord(w) as fake:
            data = Path(tempfile.mkdtemp())
            cwd = Path(tempfile.mkdtemp())
            env = {"DISCORD_TOKEN": TOKEN, "DISCORD_API_BASE": fake.url, "DISCORD_SKILL_DATA": str(data), "DISCORD_REQUEST_GAP": "0"}
            runs = [run_cli(*args, env=env, cwd=str(cwd)) for args in
                    (("doctor",), ("servers",), ("channels", "Lab"), ("read", str(ch)), ("search", "Lab", "--content", "text"),
                     ("get", "users/@me"), ("read", "999999999999999999"), ("get", "nowhere"))]
            fake.token = "rotated"
            runs.append(run_cli("read", str(ch), env=env, cwd=str(cwd)))
        printed = "".join(r.stdout + r.stderr for r in runs)
        written = "".join(p.read_text(errors="replace") for p in [*cwd.rglob("*"), *data.rglob("*")] if p.is_file() and p.suffix != ".db")
        self.assertNotIn(TOKEN, printed)
        self.assertNotIn(TOKEN, written)
        self.assertNotIn(TOKEN, (data / "discord.db").read_bytes().decode("latin-1"))
        self.assertEqual(json.loads(runs[-1].stdout)["error"], "auth")


if __name__ == "__main__":
    unittest.main()
