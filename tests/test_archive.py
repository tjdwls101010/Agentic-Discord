"""The archive's contract: the schema enforces what its comments promise, and a page's write keeps messages and coverage true.

Pages here are built by hand to state each rule's case directly; tests/test_coverage_model.py drives the real collector against a toy channel.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from support.harness import engine

archive = engine("discord.archive")
records = engine("discord.records")

CH = 1000
USER = records.User(id=7, username="ana", global_name="Ana")


def msg(i: int, channel: int = CH, **kw) -> "records.Message":
    base = dict(id=i, channel_id=channel, author=USER, sent_at="2026-01-01T00:00:00.000000Z", kind="message", type=0,
                content=f"m{i}", raw={"id": str(i), "channel_id": str(channel)})
    base.update(kw)
    return records.Message(**base)


def page(ids, *, fetched_at, requested=100, received=None, channel=CH, **kw):
    received = set(ids) if received is None else set(received)
    return archive.Page(channel_id=channel, messages=tuple(msg(i, channel) for i in ids if i in received), fetched_at=fetched_at,
                        requested=requested, received_ids=frozenset(received), **kw)


class ArchiveCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.a = archive.Archive(self.dir / "discord.db")
        self.addCleanup(self.a.close)
        self.a.save_channels([records.Channel(id=CH, kind="text", name="general")], "2026-01-01T00:00:00.000000Z")

    def cov(self, channel: int = CH):
        return [(r.lo, r.hi, r.head_at is not None) for r in self.a.coverage(channel)]

    def absent(self):
        return {r[0] for r in self.a.db.execute("SELECT id FROM messages WHERE absent_at IS NOT NULL")}


class Schema(ArchiveCase):
    def test_every_table_is_strict(self) -> None:
        tables = self.a.db.execute("SELECT name, strict FROM pragma_table_list WHERE schema='main' AND type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        self.assertEqual({n for n, _ in tables}, {"servers", "channels", "roles", "users", "members", "messages", "coverage"})
        self.assertTrue(all(s == 1 for _, s in tables), tables)

    def test_closed_sets_foreign_keys_and_coverage_invariants_are_enforced(self) -> None:
        db = self.a.db
        db.execute("INSERT INTO users VALUES (7,'ana',NULL,0,'t')")
        bad = [
            "INSERT INTO messages (id, channel_id, author_id, sent_at, kind, content, raw, fetched_at) VALUES (1,1000,7,'t','post','', '{}','t')",
            "INSERT INTO channels (id, kind, fetched_at) VALUES (5,'chat','t')",
            "INSERT INTO messages (id, channel_id, author_id, sent_at, kind, content, raw, fetched_at) VALUES (2,999,7,'t','message','', '{}','t')",
            "INSERT INTO messages (id, channel_id, author_id, sent_at, kind, content, raw, fetched_at) VALUES (3,1000,8,'t','message','', '{}','t')",
            "INSERT INTO messages (id, channel_id, author_id, sent_at, kind, content, raw, fetched_at) VALUES (4,1000,7,'t','message','', 'not json','t')",
            "INSERT INTO users VALUES (9,'b',NULL,2,'t')",
            "INSERT INTO users VALUES ('x','b',NULL,0,'t')",
        ]
        for sql in bad:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                db.execute(sql)
        db.execute("INSERT INTO coverage VALUES (1000, 10, 20, 't', 't', 20)")
        db.execute("INSERT INTO channels (id, kind, fetched_at) VALUES (1001, 'text', 't')")
        for sql in ("INSERT INTO coverage VALUES (1000, 15, 30, 't', NULL, NULL)",   # overlaps
                    "INSERT INTO coverage VALUES (1000, 21, 30, 't', NULL, NULL)",   # touches
                    "INSERT INTO coverage VALUES (1000, 1, 9, 't', NULL, NULL)",     # touches below
                    "INSERT INTO coverage VALUES (1000, 40, 50, 't', 'u', 45)",      # a second head
                    "INSERT INTO coverage VALUES (1000, 60, 50, 't', NULL, NULL)",   # lo > hi
                    "INSERT INTO coverage VALUES (1000, 40, 50, 't', NULL, 45)",     # head_id without head_at
                    "INSERT INTO coverage VALUES (1001, 40, 50, 't', 'u', 51)"):     # head_id outside the range
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                db.execute(sql)
        db.execute("INSERT INTO coverage VALUES (1000, 40, 50, 't', NULL, NULL)")
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE coverage SET hi_id = 39 WHERE lo_id = 10")

    def test_every_comment_in_the_schema_file_survives_into_the_database(self) -> None:
        """SQLite keeps comments only inside a CREATE statement; `.schema` must show the same explanation as the file."""
        stored = "\n".join(r[0] for r in self.a.db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        comments = [line.strip() for line in archive.SCHEMA.read_text(encoding="utf-8").splitlines() if "--" in line]
        self.assertGreater(len(comments), 30)
        missing = [c for c in comments if re.sub(r"^.*?--", "--", c) not in stored]
        self.assertEqual(missing, [])

    def test_messages_named_shows_nick_then_global_name_then_username(self) -> None:
        db = self.a.db
        db.execute("INSERT INTO servers VALUES (1,'s','t')")
        db.execute("UPDATE channels SET server_id=1 WHERE id=1000")
        db.executemany("INSERT INTO users VALUES (?,?,?,0,'t')", [(1, "u1", "G1"), (2, "u2", "G2"), (3, "u3", None)])
        db.execute("INSERT INTO members VALUES (1,1,'Nick1',1,'t')")
        db.execute("INSERT INTO members VALUES (1,2,NULL,1,'t')")
        for i, author in ((11, 1), (12, 2), (13, 3)):
            db.execute("INSERT INTO messages (id, channel_id, author_id, sent_at, kind, content, raw, fetched_at) VALUES (?,1000,?,'t','message','','{}','t')", (i, author))
        self.assertEqual(db.execute("SELECT author FROM messages_named ORDER BY id").fetchall(), [("Nick1",), ("G2",), ("u3",)])

    def test_a_different_schema_version_is_refused_not_written(self) -> None:
        self.a.db.execute("PRAGMA user_version = 99")
        self.a.close()
        errors = engine("discord.errors")
        with self.assertRaises(errors.Refusal):
            archive.Archive(self.dir / "discord.db")
        self.a = archive.Archive(self.dir / "other.db")
        self.addCleanup(self.a.close)


class PageRules(ArchiveCase):
    """Rules (a)-(h) and the scenarios the plan requires, at the level of single pages."""

    def test_an_oldest_first_walk_proves_from_after_plus_one(self) -> None:
        self.a.record_page(page([1010, 1020], fetched_at="t1", requested=2, after=1005))
        self.assertEqual(self.cov(), [(1006, 1020, False)])

    def test_a_short_oldest_first_page_reaches_the_head(self) -> None:
        self.a.record_page(page([1010, 1020], fetched_at="t1", requested=100, after=1005))
        self.assertEqual(self.cov(), [(1006, 1020, True)])

    def test_a_short_newest_first_page_reaches_the_channel_start(self) -> None:
        self.a.record_page(page([1010, 1020], fetched_at="t1", requested=100))
        self.assertEqual(self.cov(), [(CH, 1020, True)])

    def test_a_truncated_newest_first_walk_records_only_what_it_stored(self) -> None:
        """Scenario 1: a walk cut by --limit records its lowest stored id, never the requested lower bound."""
        self.a.record_page(page([1050, 1060], fetched_at="t1", requested=2, before=1100, before_is_past=True))
        self.assertEqual(self.cov(), [(1050, 1099, False)])

    def test_before_proves_up_to_before_minus_one_only_when_past(self) -> None:
        self.a.record_page(page([1050], fetched_at="t1", requested=1, before=1100, before_is_past=False))
        self.assertEqual(self.cov(), [(1050, 1050, False)])

    def test_the_head_upper_bound_is_the_newest_received_not_now(self) -> None:
        """Scenario 2: a later message lies above the head range, so --new will fetch it."""
        self.a.record_page(page([1050, 1060], fetched_at="t1", requested=2))
        self.assertEqual(self.a.head(CH).hi, 1060)

    def test_rereading_part_of_a_range_marks_absence_only_inside_that_part(self) -> None:
        """Scenario 3."""
        self.a.record_page(page([1010, 1020, 1030, 1040], fetched_at="t1", requested=100))
        self.a.record_page(page([1020], fetched_at="t2", requested=1, after=1015, before=1025))
        self.assertEqual(self.absent(), set())
        self.a.record_page(page([1020], fetched_at="t3", requested=100, after=1011, before=1035, before_is_past=True,
                                received=[1020, 1040]))
        self.assertEqual(self.absent(), {1030})

    def test_an_around_window_never_becomes_the_head(self) -> None:
        """Scenario 4."""
        self.a.record_page(page([1010, 1020], fetched_at="t1", requested=2))
        self.a.record_page(page([1080, 1090], fetched_at="t2", requested=2, around=True))
        self.assertEqual(self.cov(), [(1010, 1020, True), (1080, 1090, False)])
        self.assertEqual(self.a.head(CH).hi, 1020)

    def test_separate_windows_are_not_merged_for_lack_of_stored_messages_between(self) -> None:
        """Scenario 5."""
        self.a.record_page(page([1010, 1020], fetched_at="t1", requested=2, around=True))
        self.a.record_page(page([1030, 1040], fetched_at="t2", requested=2, around=True))
        self.assertEqual(self.cov(), [(1010, 1020, False), (1030, 1040, False)])
        self.a.record_page(page([1021], fetched_at="t3", requested=1, around=True))
        self.assertEqual(self.cov(), [(1010, 1021, False), (1030, 1040, False)])

    def test_an_empty_answer_between_past_bounds_fills_the_gap(self) -> None:
        """Scenario 6: the one message between two ranges was deleted; the empty read still proves the gap."""
        self.a.record_page(page([1010, 1020], fetched_at="t1", requested=2, around=True))
        self.a.record_page(page([1030], fetched_at="t1", requested=1, around=True))
        self.a.db.execute("INSERT INTO messages (id, channel_id, author_id, sent_at, kind, content, raw, fetched_at) VALUES (1025,1000,7,'t','message','','{}','t0')")
        self.a.record_page(page([], fetched_at="t2", requested=100, after=1020, before=1030, before_is_past=True, received=[1030, 1031]))
        self.assertEqual(self.cov(), [(1010, 1030, False)])
        self.assertEqual(self.absent(), {1025})

    def test_strays_change_neither_coverage_nor_existing_rows(self) -> None:
        """Scenario 8."""
        self.a.record_page(page([1010], fetched_at="t1", requested=1, around=True))
        self.a.db.execute("UPDATE messages SET absent_at='t2' WHERE id=1010")
        self.a.add_strays([msg(1010, content="edited"), msg(1500)], "t3")
        self.assertEqual(self.cov(), [(1010, 1010, False)])
        row = self.a.db.execute("SELECT content, absent_at, fetched_at FROM messages WHERE id=1010").fetchone()
        self.assertEqual(row, ("m1010", "t2", "t1"))
        self.assertEqual(self.a.db.execute("SELECT count(*) FROM messages WHERE id=1500").fetchone()[0], 1)

    def test_a_row_stored_after_the_page_was_requested_is_not_marked_absent(self) -> None:
        """Scenario 10: another run stored 1015 after this page was requested; this page's answer cannot speak for it."""
        self.a.add_strays([msg(1015)], "t5")
        self.a.record_page(page([1010, 1020], fetched_at="t4", requested=100, after=1005))
        self.assertEqual(self.absent(), set())

    def test_a_reread_clears_absence_and_an_older_answer_does_not_overwrite_a_newer_one(self) -> None:
        self.a.record_page(page([1010], fetched_at="t1", requested=1, around=True))
        self.a.db.execute("UPDATE messages SET absent_at='t2' WHERE id=1010")
        self.a.record_page(page([1010], fetched_at="t3", requested=1, around=True))
        self.assertEqual(self.absent(), set())
        self.a.record_page(archive.Page(channel_id=CH, messages=(msg(1010, content="old"),), fetched_at="t0", requested=1,
                                        received_ids=frozenset({1010}), around=True))
        self.assertEqual(self.a.db.execute("SELECT content FROM messages WHERE id=1010").fetchone()[0], "m1010")

    def test_thread_starters_and_forum_first_posts_stay_in_their_own_channels(self) -> None:
        """Scenario 11: a text thread's type-21 stub has its own id and points at the original in the parent; a forum post's first message has the thread's id and lives in the thread."""
        thread, forum_post = 1100, 2000
        self.a.save_channels([records.Channel(id=thread, kind="thread", name="t", parent_id=CH),
                              records.Channel(id=1500, kind="forum", name="f"),
                              records.Channel(id=forum_post, kind="thread", name="p", parent_id=1500)], "t0")
        original = msg(thread, CH, content="the original")
        stub = msg(1105, thread, kind="thread_starter", type=21, content="", referenced=original)
        self.a.record_page(archive.Page(channel_id=thread, messages=(stub,), fetched_at="t1", requested=100, received_ids=frozenset({1105})))
        first = msg(forum_post, forum_post, content="post body")
        self.a.record_page(archive.Page(channel_id=forum_post, messages=(first,), fetched_at="t1", requested=100, received_ids=frozenset({forum_post})))
        rows = dict(self.a.db.execute("SELECT id, channel_id FROM messages"))
        self.assertEqual(rows, {thread: CH, 1105: thread, forum_post: forum_post})
        self.assertEqual(self.cov(thread), [(thread, 1105, True)])
        self.assertEqual(self.cov(CH), [])

    def test_a_message_in_a_channel_never_listed_gets_an_unknown_placeholder(self) -> None:
        self.a.add_strays([msg(3001, channel=3000)], "t1")
        self.assertEqual(self.a.db.execute("SELECT kind, name FROM channels WHERE id=3000").fetchone(), ("unknown", None))

    def test_an_empty_oldest_first_page_marks_the_range_holding_after_as_head(self) -> None:
        self.a.record_page(page([1010, 1020], fetched_at="t1", requested=2, around=True))
        self.a.record_page(page([], fetched_at="t2", requested=100, after=1020))
        self.assertEqual(self.cov(), [(1010, 1020, True)])

    def test_a_range_merged_into_the_head_keeps_the_head_where_it_was_seen(self) -> None:
        """A later window that touches the head extends the range but is no evidence of a newer head: --new still continues after the newest message the head read found."""
        self.a.record_page(page([1010, 1020], fetched_at="t1", requested=2))
        self.a.record_page(page([1021, 1030], fetched_at="t2", requested=2, around=True))
        head = self.a.head(CH)
        self.assertEqual((head.lo, head.hi, head.head_id, head.head_at), (1010, 1030, 1020, "t1"))

    def test_an_empty_walk_to_a_past_bound_puts_the_head_where_messages_ended(self) -> None:
        """Nothing after 1005 up to 1999: the walk reached the end at 1006, not at 1999, which is only a time bound."""
        self.a.record_page(page([], fetched_at="t2", requested=100, after=1005, before=2000, before_is_past=True))
        head = self.a.head(CH)
        self.assertEqual((head.lo, head.hi, head.head_id), (1006, 1999, 1006))

    def test_an_answer_older_than_an_absence_does_not_clear_it(self) -> None:
        """A slow read requested at t1 commits after another read at t2 found the message gone."""
        self.a.record_page(page([1010], fetched_at="t0", requested=1, around=True))
        self.a.db.execute("UPDATE messages SET absent_at='t2' WHERE id=1010")
        self.a.record_page(page([1010], fetched_at="t1", requested=1, around=True))
        self.assertEqual(self.absent(), {1010})
        self.a.record_page(page([1010], fetched_at="t3", requested=1, around=True))
        self.assertEqual(self.absent(), set())

    def test_merging_keeps_the_oldest_fetch_time(self) -> None:
        self.a.record_page(page([1010, 1020], fetched_at="t5", requested=2, around=True))
        self.a.record_page(page([1021, 1030], fetched_at="t1", requested=2, around=True))
        self.assertEqual([r.oldest_fetched_at for r in self.a.coverage(CH)], ["t1"])


class ResultFiles(unittest.TestCase):
    def test_each_call_gets_a_new_file(self) -> None:
        d = Path(tempfile.mkdtemp())
        paths = {archive.write_result(d, "read", f"x{i}") for i in range(20)}
        self.assertEqual(len(paths), 20)
        self.assertEqual(sorted(p.read_text() for p in paths), sorted(f"x{i}" for i in range(20)))


if __name__ == "__main__":
    unittest.main()
