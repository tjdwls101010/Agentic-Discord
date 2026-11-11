"""The archive's promises, checked against a toy channel that knows the truth.

A toy Discord (tests/support/fake_discord.py) pages 100 at a time, newest first, and between any two pages posts and deletes messages. The real collector reads it in random ways -- --last, --after with and without --before, --before alone, --around, --new, with and without --skip-covered, with small limits -- and after every read the archive is compared with the toy's own record of which messages exist:

1. coverage is true: every message alive in the toy whose id lies in a coverage range of its channel is stored and not marked absent;
2. absence is true: every row marked absent was deleted in the toy;
3. --new misses nothing: a --new that was not cut stores every message alive in the toy above the newest message an earlier read took while walking from the channel's newest (--last, or an --after or --new that ran to the end) -- tracked here from the toy, not from the archive's head mark;
4. a read returns its range: a read by --after/--before that was not cut returns every message in its range that existed throughout the read.

The oracle is the toy's state, never the archive's own bookkeeping. `MutantsAreCaught` breaks one coverage rule at a time in a copy of the skill and requires this fuzz to fail on it.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from support.fake_discord import TOKEN, FakeDiscord, ToyWorld, snowflake
from support.harness import SCRIPTS, engine

api = engine("discord.discord_api")
archive = engine("discord.archive")
reading = engine("discord.reading")
errors = engine("discord.errors")

SEEDS = int(os.environ.get("DISCORD_FUZZ_SEEDS", "300"))


class Run:
    """One seed: a toy world, the fake server, a fresh archive and a random sequence of reads."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.world = ToyWorld()
        w = self.world
        self.author = w.user("ana")
        self.channel = w.channel("general", guild=w.guild("g"))
        for _ in range(self.rng.choice([0, 3, 99, 100, 101, 250, 420])):
            w.post(self.channel, self.author, "m")
            w.tick(self.rng.choice([0.001, 1, 60, 3600]))
        self.fake = FakeDiscord(w).__enter__()
        self.fake.before_answer = self.between_pages
        self.dir = Path(tempfile.mkdtemp())
        self.db = archive.Archive(self.dir / "discord.db")
        self.api = api.Discord(api.Http(TOKEN, token_hint="x", base_url=self.fake.url, gap=0))
        self.db.save_channels([self.api.channel(self.channel)], api.iso(w.now))
        self.log: list[str] = []
        self.true_head: int | None = None

    def close(self) -> None:
        self.db.close()
        self.fake.__exit__()
        shutil.rmtree(self.dir, ignore_errors=True)

    def between_pages(self, path: str, q) -> None:
        w = self.world
        w.tick(0.05)
        if "/messages" not in path:
            return
        r = self.rng.random()
        if r < 0.15:
            for _ in range(self.rng.randint(1, 5)):
                w.post(self.channel, self.author, "new")
        elif r < 0.30:
            alive = sorted(w.alive(self.channel))
            for mid in self.rng.sample(alive, min(len(alive), self.rng.randint(1, 4))):
                w.delete(mid)
        w.tick(0.05)

    def some_id(self) -> int:
        """A bound: an existing message, a deleted one, an id between messages, or a time in the past or the future."""
        w = self.world
        known = sorted(w.created)
        r = self.rng.random()
        if known and r < 0.25:
            return self.rng.choice(known[-120:])   # near the newest, where --new and the head live
        if known and r < 0.5:
            return self.rng.choice(known)
        if known and r < 0.7:
            return self.rng.choice(known) + self.rng.choice([-1, 1, 1 << 22])
        return snowflake(w.now + timedelta(seconds=self.rng.choice([-86400 * 30, -3600, -30, 5, 600])))

    def step(self) -> None:
        w, rng = self.world, self.rng
        if rng.random() < 0.2:
            w.tick(rng.choice([1, 120]))
            for _ in range(rng.randint(0, 120)):
                w.post(self.channel, self.author, "later")
        limit = rng.choice([1, 2, 99, 100, 101, 150, 1000])
        skip = rng.random() < 0.4
        mode = rng.choice(["last", "after", "afterbefore", "before", "around", "new", "new"])
        if mode == "last":
            spec = reading.Spec("last", limit, skip_covered=skip)
        elif mode == "after":
            spec = reading.Spec("after", limit, after=self.some_id(), skip_covered=skip)
        elif mode == "afterbefore":
            a, b = sorted([self.some_id(), self.some_id()])
            if a == b:
                b += 1
            spec = reading.Spec("after", limit, after=a, before=b, skip_covered=skip)
        elif mode == "before":
            spec = reading.Spec("before", limit, before=self.some_id(), skip_covered=skip)
        elif mode == "around":
            spec = reading.Spec("around", min(limit, 100), around=self.some_id())
        else:
            spec = reading.Spec("new", limit, skip_covered=skip)
        head_before = self.true_head
        alive_at_start = set(self.world.alive(self.channel))
        self.log.append(f"{spec} head={head_before}")
        last = int(self.world.channels[self.channel].get("last_message_id") or 0) or None
        try:
            out = reading.collect(self.api, self.db, self.channel, spec, last_message_id=last, now=lambda: w.now)
        except errors.Refusal:
            if spec.mode != "new" or self.db.head(self.channel) is not None:
                raise
            return
        ids = [m.id for m in out.messages]
        walked_from_newest = spec.mode == "last" or (spec.mode in ("after", "new") and spec.before is None and out.cut is None)
        if ids and walked_from_newest:
            self.true_head = max(ids + ([self.true_head] if self.true_head else []))
        self.check(spec, out, head_before, alive_at_start)

    def check(self, spec, out, head_before, alive_at_start) -> None:
        alive = self.world.alive(self.channel)
        stored = {r[0]: r[1] for r in self.db.db.execute("SELECT id, absent_at FROM messages WHERE channel_id=?", (self.channel,))}
        problems = []
        for r in self.db.coverage(self.channel):
            missing = [i for i in alive if r.lo <= i <= r.hi and (i not in stored or stored[i] is not None)]
            if missing:
                problems.append(f"coverage [{r.lo}, {r.hi}] claims {len(missing)} messages it does not hold, e.g. {missing[:3]}")
        wrongly_absent = [i for i, a in stored.items() if a is not None and i in alive]
        if wrongly_absent:
            problems.append(f"marked absent but alive: {wrongly_absent[:3]}")
        if spec.mode == "new" and head_before is not None and out.cut is None:
            skipped = [i for i in alive if i > head_before and (i not in stored or stored[i] is not None)]
            if skipped:
                problems.append(f"--new skipped {len(skipped)} messages above the old head {head_before}, e.g. {skipped[:3]}")
        if spec.mode in ("after", "before") and out.cut is None:
            lo = spec.after if spec.after is not None else -1
            hi = spec.before if spec.before is not None else float("inf")
            got = {m.id for m in out.messages}
            lost = sorted(i for i in alive_at_start & alive if lo < i < hi and i not in got)
            if lost:
                problems.append(f"the read left out {len(lost)} messages of its range, e.g. {lost[:3]}")
        if problems:
            raise AssertionError("\n".join(problems + ["after:"] + self.log[-6:]))


class Fuzz(unittest.TestCase):
    def test_the_archive_stays_true_under_random_reads_while_the_channel_changes(self) -> None:
        for seed in range(SEEDS):
            run = Run(seed)
            try:
                for _ in range(run.rng.randint(4, 14)):
                    run.step()
            except AssertionError as e:
                raise AssertionError(f"seed {seed}: {e}") from None
            finally:
                run.close()


#: (file under the package, text to replace, replacement): one broken rule each.
MUTANTS = {
    "after bound includes the cursor": ("archive/coverage.py", "lo = page.after + 1", "lo = page.after"),
    "a cut page claims the channel start": ("archive/coverage.py", "lo = page.channel_id if short else", "lo = page.channel_id if True else"),
    "a future before counts as past": ("archive/coverage.py", "if page.before is not None and page.before_is_past:", "if page.before is not None:"),
    "the head range reaches into the future": ("archive/coverage.py", "    elif ids:\n        hi = max(ids)\n    else:\n        return None, False", "    elif ids:\n        hi = max(ids) + (1 << 40)\n    else:\n        return None, False"),
    "separate ranges merge": ("archive/store.py", "reach = (channel_id, hi + 1, lo - 1)", "reach = (channel_id, hi + (1 << 40), lo - (1 << 40))"),
    "an around window becomes the head": ("archive/coverage.py", "return (Proof(min(ids), max(ids), False) if ids else None), False", "return (Proof(min(ids), max(ids), True) if ids else None), True"),
    "absence is marked outside the proven range": ("archive/store.py", "SET absent_at=? WHERE channel_id=? AND id BETWEEN ? AND ?", "SET absent_at=? WHERE channel_id=? AND id BETWEEN ? - (1 << 40) AND ?"),
    "--skip-covered jumps past the end of a range": ("reading/collect.py", "                    cursor = r.hi\n", "                    cursor = r.hi + (1 << 30)\n"),
    "--skip-covered drops a covered range's stored messages": ("reading/collect.py", "stored_raw(channel_id, r.lo, cursor - 1)", "stored_raw(channel_id, r.lo + 1, cursor - 1)"),
}


@unittest.skipIf(os.environ.get("DISCORD_SKILL_SCRIPTS"), "already running against a copy")
class MutantsAreCaught(unittest.TestCase):
    def test_each_broken_rule_turns_the_fuzz_red(self) -> None:
        tests = Path(__file__).resolve().parent
        survivors = []
        for name, (rel, old, new) in MUTANTS.items():
            with self.subTest(mutant=name):
                copy = Path(tempfile.mkdtemp()) / "scripts"
                shutil.copytree(SCRIPTS, copy, ignore=shutil.ignore_patterns("__pycache__", ".env"))
                target = copy.joinpath("discord", *rel.split("/"))  # mutation: edits a copy of the package's source
                text = target.read_text(encoding="utf-8")
                self.assertEqual(text.count(old), 1, f"mutant {name!r} no longer applies: the code it breaks changed")
                target.write_text(text.replace(old, new), encoding="utf-8")
                env = {**os.environ, "DISCORD_SKILL_SCRIPTS": str(copy), "DISCORD_FUZZ_SEEDS": str(SEEDS)}
                r = subprocess.run([sys.executable, "-m", "unittest", "test_coverage_model.Fuzz"], cwd=tests, env=env,
                                   capture_output=True, text=True, timeout=600)
                shutil.rmtree(copy.parent, ignore_errors=True)
                if r.returncode == 0:
                    survivors.append(name)
        self.assertEqual(survivors, [])


if __name__ == "__main__":
    unittest.main()
