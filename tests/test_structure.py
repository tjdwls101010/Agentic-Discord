"""The skill's tree is an interface read before any file, so its shape is checked here rather than described.

`scripts/` holds `cli.py`, the one entry point, and `discord/`, the one package -- plus `.env`, the token file, which is an agreed exception to that layout: the user chose to keep the token next to the CLI rather than in the keychain, and the file is gitignored and must be mode 600. Every unit directly in the package has a kind in `KINDS`; imports run features -> systems and stores -> shared, never back; nothing outside a unit reaches past its interface (a subpackage's `__all__`, a module's names without a leading underscore); nothing in the skill edits `sys.path`; the only non-Python file in the package is `archive/schema.sql`.

Every checker is a function of a tree or a source text, and each is first shown failing on a small tree or source built to break it.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.harness import REPO, SCRIPTS, SKILL_DIR

PACKAGE_NAME = "discord"
TESTS = Path(__file__).resolve().parent

#: The kind of every unit directly in the package. A new unit fails until it is placed here on purpose.
KINDS = {
    "reading": "feature", "directory": "feature", "doctor": "feature", "attachments": "feature",
    "discord_api": "system",
    "archive": "store", "credentials": "store",
    "records": "shared", "transcript": "shared", "errors": "shared",
}

#: What each kind may import besides its own unit.
MAY_IMPORT = {"feature": {"system", "store", "shared"}, "system": {"shared"}, "store": {"shared"}, "shared": {"shared"}}

#: The imports between features: reading names the people in what it prints through directory; attachments gets a message again for new links through reading's collector.
FEATURE_EDGES = {("reading", "directory"), ("attachments", "reading")}

#: Shared units are leaves except for these: records and transcript may raise a Refusal, transcript renders records.
SHARED_EDGES = {("transcript", "records"), ("transcript", "errors"), ("records", "errors")}

#: The only file in the package that is not Python: the archive's schema, run as-is to create the database.
NON_PYTHON = {"archive/schema.sql"}

#: What `scripts/` may hold. `.env` is the agreed exception described above.
SCRIPTS_ENTRIES = {"cli.py", PACKAGE_NAME}
SCRIPTS_OPTIONAL = {".env"}


# -- reading a tree -------------------------------------------------------------------------------


def package_files(package: Path) -> list[Path]:
    return sorted(p for p in package.rglob("*.py") if "__pycache__" not in p.parts)


def unit_of(path: Path, package: Path) -> str:
    rel = path.relative_to(package)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def units(package: Path) -> set[str]:
    return ({p.stem for p in package.glob("*.py") if p.stem != "__init__"}
            | {p.name for p in package.iterdir() if p.is_dir() and (p / "__init__.py").exists()})


def interface(package: Path, unit: str) -> set[str]:
    """A subpackage's literal `__all__`, or a module's top-level names without a leading underscore."""
    init = package / unit / "__init__.py"
    if init.exists():
        for node in ast.parse(init.read_text(encoding="utf-8")).body:
            target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
            if isinstance(target, ast.Name) and target.id == "__all__" and node.value is not None:
                return set(ast.literal_eval(node.value))
        return set()
    names: set[str] = set()
    for node in ast.parse((package / f"{unit}.py").read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return {n for n in names if not n.startswith("_")}


def resolve(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    if not node.level:
        return node.module or ""
    base = module.split(".")
    base = base[: len(base) - node.level + (1 if is_package else 0)]
    return ".".join(base + ([node.module] if node.module else []))


def imported(source: str, module: str, is_package: bool) -> list[tuple[str, list[str], int]]:
    """Every import as (absolute target, names taken, line), relative ones resolved against `module`."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out += [(a.name, [], node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append((resolve(node, module, is_package), [a.name for a in node.names], node.lineno))
    return out


def unit_named(dotted: str) -> str | None:
    parts = dotted.split(".")
    return parts[1] if parts[0] == PACKAGE_NAME and len(parts) > 1 else None


def module_of(path: Path, scripts: Path) -> tuple[str, bool]:
    rel = path.relative_to(scripts).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]), True
    return ".".join(parts), False


# -- the checkers ---------------------------------------------------------------------------------


def tree_violations(scripts: Path) -> list[str]:
    package = scripts / PACKAGE_NAME
    out = []
    top = {p.name for p in scripts.iterdir() if p.name != "__pycache__"}
    if not SCRIPTS_ENTRIES <= top or top - SCRIPTS_ENTRIES - SCRIPTS_OPTIONAL:
        out.append(f"scripts/ holds {sorted(top)}, not cli.py, {PACKAGE_NAME}/ and optionally .env")
    if PACKAGE_NAME in sys.stdlib_module_names:
        out.append(f"{PACKAGE_NAME} shadows a standard-library module")
    found = units(package)
    out += [f"unit {u} has no kind in KINDS" for u in sorted(found - set(KINDS))]
    out += [f"KINDS names {u}, which does not exist" for u in sorted(set(KINDS) - found)]
    for p in sorted(package.rglob("*")):
        rel = p.relative_to(package).as_posix()
        if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts and rel not in NON_PYTHON:
            out.append(f"{rel} is not Python and not an allowed data file")
    for d in sorted(p for p in package.rglob("*") if p.is_dir() and p.name != "__pycache__"):
        if not (d / "__init__.py").exists() and any(d.glob("*.py")):
            out.append(f"{d.relative_to(package)} holds modules but is not a package")
    return out


def may_import(mine: str, theirs: str) -> bool:
    a, b = KINDS.get(mine), KINDS.get(theirs)
    if a is None or b is None:
        return False
    if a == "feature" and b == "feature":
        return (mine, theirs) in FEATURE_EDGES
    if a == "shared" and b == "shared":
        return (mine, theirs) in SHARED_EDGES
    return b in MAY_IMPORT[a]


def direction_violations(scripts: Path) -> list[str]:
    package = scripts / PACKAGE_NAME
    out = []
    for path in package_files(package):
        module, is_pkg = module_of(path, scripts)
        mine = unit_named(module)
        for target, names, line in imported(path.read_text(encoding="utf-8"), module, is_pkg):
            targets = [target] + ([f"{target}.{n}" for n in names] if target == PACKAGE_NAME else [])
            for t in targets:
                theirs = unit_named(t)
                if theirs is None or theirs == mine or mine is None:
                    continue
                if t == f"{PACKAGE_NAME}.__version__":
                    continue
                if not may_import(mine, theirs):
                    out.append(f"{module}:{line} imports {theirs} ({KINDS.get(mine)} -> {KINDS.get(theirs)})")
    return out


def reach_violations(files: list[Path], scripts: Path) -> list[str]:
    """An import of a module inside another unit, a name taken from a unit outside its interface, or an attribute of a bound unit outside it."""
    package = scripts / PACKAGE_NAME
    known = units(package)
    out = []
    for path in files:
        if path.is_relative_to(package):
            module, is_pkg = module_of(path, scripts)
            mine = unit_named(module)
        else:
            module, is_pkg, mine = f"tests.{path.stem}", False, None
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bound: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    parts = a.name.split(".")
                    if parts[0] != PACKAGE_NAME or len(parts) < 2 or parts[1] == mine:
                        continue
                    if len(parts) > 2:
                        out.append(f"{path.name}:{node.lineno} imports {a.name}, inside a unit")
                    elif a.asname:
                        bound[a.asname] = parts[1]
            elif isinstance(node, ast.ImportFrom):
                target = resolve(node, module, is_pkg)
                parts = target.split(".")
                if parts[0] != PACKAGE_NAME:
                    continue
                if target == PACKAGE_NAME:
                    for a in node.names:
                        if a.name in known and a.name != mine:
                            bound[a.asname or a.name] = a.name
                    continue
                theirs = parts[1]
                if theirs == mine:
                    continue
                if len(parts) > 2:
                    out.append(f"{path.name}:{node.lineno} imports {target}, inside a unit")
                    continue
                public = interface(package, theirs) if theirs in known else set()
                out += [f"{path.name}:{node.lineno} takes {theirs}.{a.name}, outside its interface"
                        for a in node.names if a.name not in public]
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                # x = engine("discord.U")
                call = node.value
                if getattr(call.func, "id", getattr(call.func, "attr", None)) == "engine" and call.args \
                        and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                    parts = call.args[0].value.split(".")
                    if parts[0] == PACKAGE_NAME and len(parts) > 2:
                        out.append(f"{path.name}:{node.lineno} engines {call.args[0].value}, inside a unit")
                    elif parts[0] == PACKAGE_NAME and len(parts) == 2:
                        for t in node.targets:
                            if isinstance(t, ast.Name):
                                bound[t.id] = parts[1]
        rebound = {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in bound \
                    and node.value.id not in rebound:
                unit = bound[node.value.id]
                if unit in known and node.attr not in interface(package, unit):
                    out.append(f"{path.name}:{node.lineno} uses {unit}.{node.attr}, outside its interface")
    return out


_PATH_READS = {"index", "count", "copy", "__contains__"}


def path_edits(source: str, where: str) -> list[str]:
    """`sys.path` changed under any name, or PYTHONPATH set in an environment handed on."""
    tree = ast.parse(source)
    paths = {"sys.path"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            paths |= {f"{a.asname}.path" for a in node.names if a.name == "sys" and a.asname}
        if isinstance(node, ast.ImportFrom) and node.module == "sys":
            paths |= {a.asname or a.name for a in node.names if a.name == "path"}
    out = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) in paths \
                and node.func.attr not in _PATH_READS:
            out.append(f"{where}:{line} changes sys.path")
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for t in targets:
                if ast.unparse(t) in paths or (isinstance(t, ast.Subscript) and ast.unparse(t.value) in paths):
                    out.append(f"{where}:{line} changes sys.path")
        if isinstance(node, ast.Constant) and node.value == "PYTHONPATH":
            out.append(f"{where}:{line} names PYTHONPATH")
    return out


def main_guards(source: str) -> bool:
    return any(isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and isinstance(n.test.left, ast.Name)
               and n.test.left.id == "__name__" for n in ast.walk(ast.parse(source)))


_PACKAGE_PATH = re.compile(rf"(^|[/\\]){PACKAGE_NAME}[/\\](?:{'|'.join(KINDS)}|__init__)\b")


def package_paths(source: str, where: str) -> list[str]:
    """A string that is a filesystem path into the package (`…/discord/archive/…`), which reaches past every interface. URLs such as `https://discord.com/api` are not paths into it."""
    out = []
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "://" not in node.value \
                and (_PACKAGE_PATH.search(node.value) or node.value == PACKAGE_NAME) \
                and MUTATION_MARK not in lines[node.lineno - 1]:
            out.append(f"{where}:{node.lineno} names {node.value!r}")
    return out


#: The one sanctioned way past every interface: a mutation test editing a copy of the package's source, marked on its line.
MUTATION_MARK = "# mutation: edits a copy of the package's source"


def exit_codes_outside_cli(scripts: Path) -> list[str]:
    return [f"{p.relative_to(scripts)}:{n.lineno}" for p in package_files(scripts / PACKAGE_NAME)
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
            if isinstance(n, ast.Call) and ast.unparse(n.func) in ("sys.exit", "exit", "os._exit")]


# -- the checkers, shown failing -------------------------------------------------------------------


def fake_tree(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


GOOD = {
    "cli.py": "from discord import reading\n",
    "discord/__init__.py": "__version__ = '0'\n",
    "discord/reading/__init__.py": "from .collect import collect\n__all__ = ['collect']\n",
    "discord/reading/collect.py": "from discord import archive, directory, errors\ndef collect(): pass\n",
    "discord/directory/__init__.py": "__all__ = ['names']\ndef names(): pass\n",
    "discord/doctor.py": "",
    "discord/attachments.py": "from discord import reading\n",
    "discord/discord_api/__init__.py": "__all__ = []\n",
    "discord/archive/__init__.py": "from .db import open_db\n__all__ = ['open_db']\n",
    "discord/archive/db.py": "def open_db(): pass\n",
    "discord/archive/schema.sql": "",
    "discord/credentials.py": "",
    "discord/records.py": "from discord.errors import Refusal\n",
    "discord/transcript.py": "from discord import records\n",
    "discord/errors.py": "class Refusal(Exception): pass\n",
}


class CheckersCatchTheirCases(unittest.TestCase):
    def test_the_good_tree_passes(self) -> None:
        root = fake_tree(GOOD)
        self.assertEqual(tree_violations(root), [])
        self.assertEqual(direction_violations(root), [])
        self.assertEqual(reach_violations(package_files(root / "discord") + [root / "cli.py"], root), [])

    def test_tree_checker_catches_stray_files_and_unplaced_units(self) -> None:
        self.assertTrue(tree_violations(fake_tree({**GOOD, "helper.py": ""})))
        self.assertTrue(tree_violations(fake_tree({**GOOD, "discord/util.py": ""})))
        self.assertTrue(tree_violations(fake_tree({**GOOD, "discord/reading/notes.md": ""})))
        self.assertTrue(tree_violations(fake_tree({**GOOD, "discord/archive/extra.sql": ""})))
        self.assertTrue(tree_violations(fake_tree({k: v for k, v in GOOD.items() if k != "discord/doctor.py"})))
        self.assertFalse(tree_violations(fake_tree({**GOOD, ".env": "DISCORD_TOKEN=x\n"})))

    def test_direction_checker_catches_each_wrong_way(self) -> None:
        cases = {
            "discord/archive/db.py": "from discord import reading\n",                 # store -> feature
            "discord/discord_api/__init__.py": "from discord import archive\n",       # system -> store
            "discord/errors.py": "from discord import transcript\n",                  # shared -> shared not listed
            "discord/directory/__init__.py": "from discord import reading\n",         # feature -> feature not listed
            "discord/doctor.py": "from .reading import collect\n",                     # relative, feature -> feature
            "discord/records.py": "import discord.discord_api\n",                     # shared -> system
        }
        for rel, src in cases.items():
            with self.subTest(rel=rel):
                self.assertTrue(direction_violations(fake_tree({**GOOD, rel: src})), src)

    def test_reach_checker_catches_each_way_past_an_interface(self) -> None:
        cases = [
            "from discord.archive.db import open_db\n",
            "import discord.archive.db\n",
            "from discord.archive import db\n",
            "from discord import archive\narchive.db\n",
            "import discord.archive as a\na.db\n",
            "a = engine('discord.archive')\na.db\n",
            "x = engine('discord.archive.db')\n",
        ]
        for src in cases:
            with self.subTest(src=src):
                root = fake_tree({**GOOD, "cli.py": src})
                self.assertTrue(reach_violations([root / "cli.py"], root), src)
        root = fake_tree({**GOOD, "cli.py": "from discord import archive\narchive.open_db()\ndef f(archive):\n    archive.x\n"})
        self.assertFalse(reach_violations([root / "cli.py"], root))

    def test_path_checker_catches_edits(self) -> None:
        for src in ("import sys\nsys.path.insert(0, 'x')\n", "import sys\nsys.path = []\n", "from sys import path\npath.append(1)\n",
                    "import sys as s\ns.path.extend([])\n", "env = {'PYTHONPATH': 'x'}\n", "import sys\nsys.path[:] = []\n"):
            self.assertTrue(path_edits(src, "x"), src)
        for src in ("import sys\nprint(sys.path)\n", "path = []\npath.append(1)\n", "import sys\nsys.path.index('x')\n"):
            self.assertFalse(path_edits(src, "x"), src)

    def test_package_path_checker_ignores_urls(self) -> None:
        self.assertTrue(package_paths("p = SCRIPTS / 'discord/archive'\n", "t"))
        self.assertTrue(package_paths("p = '/x/discord/archive/schema.sql'\n", "t"))
        self.assertTrue(package_paths("p = SCRIPTS / 'discord'\n", "t"))
        self.assertFalse(package_paths("p = '.claude/skills/discord/data/captures/'\n", "t"))
        self.assertFalse(package_paths("u = 'https://discord.com/api/v9'\n", "t"))
        self.assertFalse(package_paths("m = 'discord.archive'\n", "t"))
        self.assertFalse(package_paths(f"f = copy.joinpath('discord', rel)  {MUTATION_MARK}\n", "t"))

    def test_entry_and_exit_checkers(self) -> None:
        self.assertTrue(main_guards("if __name__ == '__main__':\n    pass\n"))
        self.assertFalse(main_guards("def main(): pass\n"))
        root = fake_tree({**GOOD, "discord/doctor.py": "import sys\nsys.exit(3)\n"})
        self.assertTrue(exit_codes_outside_cli(root))


# -- the real tree ---------------------------------------------------------------------------------


def test_files() -> list[Path]:
    return sorted(p for p in TESTS.rglob("*.py") if "__pycache__" not in p.parts)


class TheSkillTree(unittest.TestCase):
    def test_tree(self) -> None:
        self.assertEqual(tree_violations(SCRIPTS), [])

    def test_imports_run_one_way(self) -> None:
        self.assertEqual(direction_violations(SCRIPTS), [])

    def test_nothing_reaches_past_an_interface(self) -> None:
        files = package_files(SCRIPTS / PACKAGE_NAME) + [SCRIPTS / "cli.py"] + [p for p in test_files() if p.name != "test_structure.py"]
        self.assertEqual(reach_violations(files, SCRIPTS), [])

    def test_tests_do_not_open_package_files_by_path(self) -> None:
        wrong = []
        for p in test_files():
            # The harness names the skill's folder, which shares the package's name; it is where every path to the skill starts.
            if p.name not in ("test_structure.py", "harness.py"):
                wrong += package_paths(p.read_text(encoding="utf-8"), str(p.relative_to(REPO)))
        self.assertEqual(wrong, [])

    def test_the_skill_never_edits_the_import_path(self) -> None:
        wrong = []
        for p in package_files(SCRIPTS / PACKAGE_NAME) + [SCRIPTS / "cli.py"]:
            wrong += path_edits(p.read_text(encoding="utf-8"), str(p.relative_to(SCRIPTS)))
        self.assertEqual(wrong, [])

    def test_cli_py_is_the_only_entry_point_and_owns_exit_codes(self) -> None:
        self.assertEqual([str(p) for p in package_files(SCRIPTS / PACKAGE_NAME) if main_guards(p.read_text(encoding="utf-8"))], [])
        self.assertEqual(exit_codes_outside_cli(SCRIPTS), [])

    def test_cli_py_declares_its_runtime(self) -> None:
        """`uv run` reads this header; without it the script runs on whatever Python is first on PATH."""
        head = (SCRIPTS / "cli.py").read_text(encoding="utf-8").split("\n# ///\n", 1)[0].splitlines()
        self.assertEqual(head[0], "# /// script")
        self.assertIn('# requires-python = ">=3.11"', head)
        self.assertIn("# dependencies = []", head)


INVOCATION = 'uv run "${CLAUDE_SKILL_DIR}/scripts/cli.py"'
QUERY = 'sqlite3 -readonly "${CLAUDE_SKILL_DIR}/data/discord.db"'


class TheSkillText(unittest.TestCase):
    """Pre-approval matches the command text, so the rules and every call the skill shows are spelled alike."""

    def test_allowed_tools_name_the_cli_and_the_read_only_archive(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        front = text.split("---", 2)[1]
        allowed = next(line for line in front.splitlines() if line.startswith("allowed-tools:"))
        self.assertEqual(allowed, f"allowed-tools: Bash({INVOCATION} *), Bash({QUERY} *)")

    def test_every_call_shown_is_spelled_like_its_rule(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        blocks = text.split("```")[1::2]
        lines = [line.strip() for block in blocks for line in block.splitlines()]
        calls = [line for line in lines if "cli.py" in line or line.startswith("sqlite3")]
        self.assertTrue(calls)
        self.assertEqual([c for c in calls if not (c.startswith(INVOCATION) or c.startswith(QUERY))], [])


class TheTokenFile(unittest.TestCase):
    """The agreed exception: `scripts/.env` lives in the tree, so git must never be able to take it."""

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)

    def test_the_token_file_and_the_archive_are_ignored(self) -> None:
        if self.git("rev-parse", "--git-dir").returncode != 0:
            self.skipTest("not a git checkout")
        for rel in (SKILL_DIR / "scripts" / ".env", SKILL_DIR / "data" / "discord.db", SKILL_DIR / "data" / "results" / "x.txt"):
            with self.subTest(rel=rel):
                self.assertEqual(self.git("check-ignore", "-q", str(rel.relative_to(REPO))).returncode, 0)

    def test_no_tracked_file_holds_a_token_shaped_string(self) -> None:
        if self.git("rev-parse", "--git-dir").returncode != 0:
            self.skipTest("not a git checkout")
        # A user token is three dot-separated base64url parts: ~24-26 chars of base64 user id, 6, then 27-38.
        token = re.compile(r"\b[A-Za-z0-9_-]{24,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,38}\b")
        hits = []
        for rel in self.git("ls-files", "-co", "--exclude-standard").stdout.splitlines():
            p = REPO / rel
            if p.is_file():
                try:
                    text = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                hits += [rel for _ in token.finditer(text)]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
