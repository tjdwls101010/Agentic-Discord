"""Raw message objects, shaped as Discord sends them, through the Discord unit's parser and the transcript: each case of the notation `read --help` documents.

Expected lines are written from the notation, not copied from output.
"""

from __future__ import annotations

import os
import time
import unittest

from support.harness import engine

api = engine("discord.discord_api")
transcript = engine("discord.transcript")

ANA = {"id": "11", "username": "ana", "global_name": "Ana"}
BOB = {"id": "12", "username": "bob", "global_name": None}
BOT = {"id": "13", "username": "MEE6", "bot": True}
CH = "500"


def raw(i: int, author=ANA, content: str = "", t: int = 0, **extra) -> dict:
    m = {"id": str(i), "channel_id": CH, "type": t, "content": content, "author": author,
         "timestamp": "2026-11-11T00:05:00+00:00", "edited_timestamp": None, "attachments": [], "embeds": [],
         "mentions": [], "mention_roles": [], "pinned": False}
    m.update(extra)
    return m


NAMES = transcript.Names(users={11: "Ana (nick)", 13: "MEE6"}, channels={500: "general", 600: "news"}, roles={900: "Mods"})


def render(*raws, show_channel=False) -> list[str]:
    return transcript.render([api.parse_message(r) for r in raws], NAMES, show_channel=show_channel).splitlines()


class LocalTime(unittest.TestCase):
    def setUp(self) -> None:
        self.old = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()

    def tearDown(self) -> None:
        if self.old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old
        time.tzset()


class Notation(LocalTime):
    def test_a_day_line_then_local_time_author_text_and_id(self) -> None:
        self.assertEqual(render(raw(1, content="hi")), ["── 2026-11-11 Wed ──", "09:05 Ana (nick): hi  [1]"])

    def test_a_new_local_day_gets_its_own_line(self) -> None:
        late = raw(2, content="later", timestamp="2026-11-11T15:30:00+00:00")   # 00:30 the next day in Seoul
        self.assertEqual(render(raw(1, content="hi"), late)[2], "── 2026-11-12 Thu ──")

    def test_names_fall_back_to_global_name_then_username(self) -> None:
        self.assertEqual(render(raw(1, author=BOB, content="x"))[1], "09:05 bob: x  [1]")

    def test_edited_and_pinned(self) -> None:
        line = render(raw(1, content="x", edited_timestamp="2026-11-11T00:06:00+00:00", pinned=True))[1]
        self.assertEqual(line, "09:05 Ana (nick) (edited; pinned): x  [1]")

    def test_continuation_lines_are_indented(self) -> None:
        self.assertEqual(render(raw(1, content="a\n\nb"))[1:], ["09:05 Ana (nick): a", "", "    b  [1]"])

    def test_channel_names_on_search_lines(self) -> None:
        self.assertEqual(render(raw(1, content="x"), show_channel=True)[1], "09:05 #general · Ana (nick): x  [1]")


class Replies(LocalTime):
    def test_a_reply_quotes_the_start_of_its_target(self) -> None:
        target = raw(1, author=BOB, content="what time is the meeting tomorrow, and where will it be held? " * 2)
        r = raw(2, content="at ten", t=19, message_reference={"message_id": "1", "channel_id": CH}, referenced_message=target)
        line = render(r)[1]
        self.assertTrue(line.startswith('09:05 Ana (nick) (reply to bob: "what time is the meeting tomorrow, and where will it be hel…"): at ten'), line)

    def test_a_reply_whose_target_was_deleted(self) -> None:
        r = raw(2, content="ok", t=19, message_reference={"message_id": "1"}, referenced_message=None)
        self.assertEqual(render(r)[1], "09:05 Ana (nick) (reply to a deleted message): ok  [2]")

    def test_a_reply_whose_target_was_not_sent_along(self) -> None:
        r = raw(2, content="ok", t=19, message_reference={"message_id": "1"})
        self.assertEqual(render(r)[1], "09:05 Ana (nick) (reply to [1]): ok  [2]")

    def test_only_type_19_is_a_reply(self) -> None:
        """Pins, thread creation, forwards and crossposts carry a message_reference too."""
        pin = raw(3, t=6, message_reference={"message_id": "1", "channel_id": CH})
        self.assertEqual(render(pin)[1], "09:05 * Ana (nick) pinned a message [1]  [3]")


class Forwards(LocalTime):
    def test_a_forward_names_its_origin_and_says_its_author_is_unknown(self) -> None:
        f = raw(4, author=BOB, message_reference={"type": 1, "channel_id": "600", "message_id": "77"},
                message_snapshots=[{"message": {"content": "breaking news", "timestamp": "2026-11-10T23:00:00+00:00", "attachments": [], "embeds": []}}])
        lines = render(f)
        self.assertEqual(lines[1], "09:05 bob: [4]".replace(": [4]", ":   [4]"))
        self.assertEqual(lines[2], "  + forwarded (from #news, message [77], sent 2026-11-11 08:00; Discord does not say who wrote it): breaking news")


class Apps(LocalTime):
    def test_an_app_answer_names_who_used_which_command(self) -> None:
        m = raw(5, author=BOT, content="Level 3", t=20, interaction_metadata={"user": ANA, "name": "rank", "type": 2})
        self.assertEqual(render(m)[1], "09:05 MEE6 (Ana (nick) used /rank): Level 3  [5]")

    def test_a_poll_and_its_result(self) -> None:
        poll = {"question": {"text": "Lunch?"}, "answers": [{"answer_id": 1, "poll_media": {"text": "Pizza"}}, {"answer_id": 2, "poll_media": {"text": "Soup"}}],
                "expiry": "2026-11-12T00:00:00+00:00", "results": {"is_finalized": True, "answer_counts": [{"id": 1, "count": 3}, {"id": 2, "count": 1}]}}
        self.assertEqual(render(raw(6, poll=poll))[2], '  + poll: "Lunch?" — Pizza 3 · Soup 1 (final)')
        open_poll = dict(poll, results=None)
        self.assertEqual(render(raw(6, poll=open_poll))[2], '  + poll: "Lunch?" — Pizza · Soup (open until 2026-11-12 09:00)')
        result = raw(7, t=46, embeds=[{"type": "poll_result", "fields": [
            {"name": "poll_question_text", "value": "Lunch?"}, {"name": "victor_answer_text", "value": "Pizza"},
            {"name": "victor_answer_votes", "value": "3"}, {"name": "total_votes", "value": "4"}]}])
        self.assertEqual(render(result)[1], '09:05 * poll "Lunch?" ended: "Pizza" won with 3 of 4 votes  [7]')


class Events(LocalTime):
    def test_system_messages_by_type(self) -> None:
        cases = {
            7: "* Ana (nick) joined the server",
            3: "* Ana (nick) started a call",
            8: "* Ana (nick) boosted the server",
        }
        for t, text in cases.items():
            with self.subTest(t=t):
                self.assertEqual(render(raw(10, t=t))[1], f"09:05 {text}  [10]")
        self.assertEqual(render(raw(10, t=4, content="new-name"))[1], "09:05 * Ana (nick) renamed the channel to new-name  [10]")
        self.assertEqual(render(raw(10, t=1, mentions=[BOB]))[1], "09:05 * Ana (nick) added bob to the group  [10]")
        self.assertEqual(render(raw(10, t=2, mentions=[ANA]))[1], "09:05 * Ana (nick) left the group  [10]")
        made = raw(10, t=18, content="Planning", message_reference={"channel_id": "800"})
        self.assertEqual(render(made)[1], "09:05 * Ana (nick) started a thread: Planning [800]  [10]")

    def test_a_type_this_version_does_not_know(self) -> None:
        self.assertEqual(render(raw(10, t=99, content="hm"))[1], "09:05 Ana (nick): [type 99] hm  [10]")
        self.assertEqual(api.parse_message(raw(10, t=99)).kind, "other")


class ThreadStarts(LocalTime):
    def test_the_first_entry_of_a_thread_shows_the_original_from_the_parent(self) -> None:
        original = raw(800, author=BOB, content="let's discuss", channel_id="500", timestamp="2026-11-10T23:00:00+00:00",
                       thread={"id": "800", "name": "discuss", "message_count": 3})
        stub = raw(801, t=21, channel_id="800", message_reference={"message_id": "800", "channel_id": "500"}, referenced_message=original)
        self.assertEqual(render(stub)[1:], ["09:05 bob (thread start; the original is [800] in #general, sent 2026-11-11 08:00): let's discuss  [801]"])

    def test_a_thread_start_whose_original_was_deleted(self) -> None:
        stub = raw(801, t=21, channel_id="800", message_reference={"message_id": "800", "channel_id": "500"}, referenced_message=None)
        self.assertEqual(render(stub)[1], "09:05 * thread start: the original message was deleted  [801]")


class Tokens(LocalTime):
    def test_mentions_emoji_and_timestamps_are_written_out(self) -> None:
        text = "hi <@11> <@!12> in <#600> <@&900> <:pepe:123> <a:dance:456> at <t:1762819200:t> on <t:1762819200:D> or <t:1762819200>"
        line = render(raw(1, content=text))[1]
        self.assertIn("hi @Ana (nick) @unknown(12) in #news @Mods :pepe: :dance: at 09:00 on 2025-11-11 or 2025-11-11 09:00", line)

    def test_ids_that_cannot_be_named_stay_visible(self) -> None:
        line = render(raw(1, content="<#999> <@&998>"))[1]
        self.assertIn("#unknown(999) @unknown-role(998)", line)

    def test_code_is_left_as_written(self) -> None:
        line = "\n".join(render(raw(1, content="see `<@11>` and\n```\n<#600>\n```")))
        self.assertIn("`<@11>`", line)
        self.assertIn("<#600>", line)

    def test_referenced_ids_cover_people_mentions_and_channels(self) -> None:
        m = api.parse_message(raw(2, content="<@12> <#600> <@&900> `<@99>`", t=19, message_reference={"message_id": "1"},
                                  referenced_message=raw(1, author=BOB)))
        users, channels, roles = transcript.referenced_ids([m])
        self.assertEqual(users, {11, 12})
        self.assertEqual(channels, {500, 600})
        self.assertEqual(roles, {900})


class Details(LocalTime):
    def test_files_embeds_stickers_reactions_threads(self) -> None:
        m = raw(1, content="look https://example.com/a",
                attachments=[{"filename": "a.png", "url": "https://cdn.discordapp.com/attachments/1/2/a.png?ex=6af677f0&is=6af52670&hm=1e41&", "content_type": "image/png"}],
                embeds=[{"url": "https://example.com/a", "title": "A", "description": "about <@11>", "fields": [{"name": "k", "value": "v"}], "footer": {"text": "f"}},
                        {"url": "https://other.example/b", "title": "B"}],
                sticker_items=[{"name": "wave"}],
                reactions=[{"emoji": {"name": "👍"}, "count": 3}, {"emoji": {"id": "55", "name": "pepe"}, "count": 1}, {"emoji": {"id": "56", "name": None}, "count": 2}],
                thread={"id": "700", "name": "more", "message_count": 4})
        self.assertEqual(render(m)[2:], [
            "  + file: a.png (image/png)",
            "  + embed: A | about @Ana (nick) | k: v | f",
            "  + embed: B | https://other.example/b",
            "  + sticker: wave",
            "  + reactions: 👍 3, :pepe: 1, :56: 2",
            "  + thread: more [700] · 4 messages",
        ])


class FileLines(LocalTime):
    """A file line says what the message declares and carries no link: links are signed and expire within a day, the archive's raw keeps them, and `files` fetches the bytes."""

    URL = "https://cdn.discordapp.com/attachments/500/9/chart.png?ex=6af677f0&is=6af52670&hm=" + "1e41" * 16 + "&"

    def test_type_size_and_pixels_as_declared_and_no_link(self) -> None:
        a = {"id": "9", "filename": "chart.png", "url": self.URL, "content_type": "image/png", "size": 263168, "width": 2096, "height": 1698}
        lines = render(raw(1, content="see https://example.com/x", attachments=[a]))
        self.assertEqual(lines[1:], ["09:05 Ana (nick): see https://example.com/x  [1]", "  + file: chart.png (image/png, 257 KB, 2096×1698)"])

    def test_what_is_not_declared_is_left_out(self) -> None:
        cases = [({"size": 900}, "  + file: n.txt (900 B)"), ({"size": 3 * 1024 * 1024 + 1}, "  + file: n.txt (3.0 MB)"), ({}, "  + file: n.txt")]
        for extra, line in cases:
            with self.subTest(extra=extra):
                self.assertEqual(render(raw(1, attachments=[{"filename": "n.txt", "url": self.URL, **extra}]))[2], line)

    def test_a_forwarded_file_is_written_the_same_way(self) -> None:
        f = raw(4, author=BOB, message_reference={"type": 1, "channel_id": "600", "message_id": "77"},
                message_snapshots=[{"message": {"content": "", "timestamp": "2026-11-10T23:00:00+00:00", "embeds": [],
                                                "attachments": [{"filename": "s.webp", "url": self.URL, "content_type": "image/webp", "size": 2048}]}}])
        self.assertEqual(render(f)[3], "  + forwarded file: s.webp (image/webp, 2 KB)")
        self.assertNotIn("cdn.discordapp.com", "\n".join(render(f)))


class WhatTheArchiveKnows(LocalTime):
    """`show` passes what only the archive knows: which rows a later read found missing, and what lies between two messages it prints next to each other."""

    def lines(self, *raws, gone=(), gaps=None) -> list[str]:
        return transcript.render([api.parse_message(r) for r in raws], NAMES, gone=set(gone), gaps=gaps or {}).splitlines()

    def test_gone_follows_the_id_of_a_post_a_system_message_and_a_thread_start(self) -> None:
        self.assertEqual(self.lines(raw(1, content="hi"), gone=[1])[1], "09:05 Ana (nick): hi  [1] (gone)")
        self.assertEqual(self.lines(raw(1, content="a\nb", edited_timestamp="2026-11-11T00:06:00+00:00"), gone=[1])[1:],
                         ["09:05 Ana (nick) (edited): a", "    b  [1] (gone)"])
        self.assertEqual(self.lines(raw(10, t=7), gone=[10])[1], "09:05 * Ana (nick) joined the server  [10] (gone)")
        original = raw(800, author=BOB, content="let's discuss", channel_id="500", timestamp="2026-11-10T23:00:00+00:00")
        stub = raw(801, t=21, channel_id="800", message_reference={"message_id": "800", "channel_id": "500"}, referenced_message=original)
        self.assertEqual(self.lines(stub, gone=[801])[1],
                         "09:05 bob (thread start; the original is [800] in #general, sent 2026-11-11 08:00): let's discuss  [801] (gone)")
        orphan = raw(801, t=21, channel_id="800", message_reference={"message_id": "800", "channel_id": "500"}, referenced_message=None)
        self.assertEqual(self.lines(orphan, gone=[801])[1], "09:05 * thread start: the original message was deleted  [801] (gone)")

    def test_gone_marks_only_the_ids_given(self) -> None:
        self.assertEqual(self.lines(raw(1, content="a"), raw(2, content="b"), gone=[2])[1:],
                         ["09:05 Ana (nick): a  [1]", "09:05 Ana (nick): b  [2] (gone)"])

    def test_a_gap_line_goes_before_its_message_and_before_a_new_day_line(self) -> None:
        late = raw(3, content="c", timestamp="2026-11-11T15:30:00+00:00")
        self.assertEqual(self.lines(raw(1, content="a"), raw(2, content="b"), late, gaps={2: "12 not shown", 3: "3 not shown; not read through"}), [
            "── 2026-11-11 Wed ──",
            "09:05 Ana (nick): a  [1]",
            "⋯ 12 not shown",
            "09:05 Ana (nick): b  [2]",
            "⋯ 3 not shown; not read through",
            "── 2026-11-12 Thu ──",
            "00:30 Ana (nick): c  [3]",
        ])

    def test_without_the_new_arguments_the_transcript_is_unchanged(self) -> None:
        self.assertEqual(render(raw(1, content="a"), raw(2, content="b"))[1:], ["09:05 Ana (nick): a  [1]", "09:05 Ana (nick): b  [2]"])


if __name__ == "__main__":
    unittest.main()
