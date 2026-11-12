# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""discord -- read Discord as the user: servers, channels, threads, messages and Discord's own search, through the user's token, read-only.

The whole command line lives here: the parser, every command's help, dispatch to the units that do the work, and the exit codes. Results go to stdout; progress and retries go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from discord import __version__, archive, credentials, directory, discord_api, doctor, reading
from discord.errors import Refusal

# Not resolved: through a symlinked install this stays the path the skill's pre-approved sqlite3 rule names.
SKILL_DIR = Path(os.path.abspath(__file__)).parents[1]
DATA_DIR = Path(os.environ.get("DISCORD_SKILL_DATA") or SKILL_DIR / "data")
#: Per-call result files live in the working directory: a session in another project cannot Read files inside the installed skill.
OUTPUT_DIR = Path.cwd() / ".discord"

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_BLOCKED = 0, 1, 2, 3

TARGET_FORMS = "an id, a discord.com/channels/… link, or a name"
TIME_FORMS = "a message id or link, 7d / 12h / 30m ago, or a local date or date-time (2026-11-01, 2026-11-01T09:30)"
EXITS = ("Exit: 0 done; 1 refused or failed (stdout is one JSON line: error, message, fix, help); "
         "2 the command line must change (same JSON line).")
NOT_A_HISTORY = ("Every fetch also goes into the archive, an SQLite database you can query, spelled exactly like this so it runs without a prompt: "
                 "sqlite3 -readonly \"{db}\" \"<SQL or .schema>\". `.schema` explains every table and what it cannot show.")


class Formatter(argparse.RawDescriptionHelpFormatter):
    """Keeps each paragraph and each argument's help on one line: whatever reads the help wraps it, not this."""

    def __init__(self, prog: str) -> None:
        super().__init__(prog, width=100_000, max_help_position=32)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # bad arguments: one JSON line, exit 2
        print(json.dumps({"error": "usage", "message": message, "help": f"{self.prog} --help"}, ensure_ascii=False))
        sys.exit(EXIT_USAGE)


def build_parser() -> Parser:
    db = DATA_DIR / "discord.db"
    p = Parser(prog="cli.py", formatter_class=Formatter,
               description="Read Discord as the user, read-only: list servers, channels and threads, read a channel's messages into a local archive and a transcript, use Discord's own search, show what the archive already holds, or GET any API path.\n\n"
                           "Every request is a GET made with the user's token, one at a time, half a second apart; `show` and SQL over the archive make no request. "
                           + NOT_A_HISTORY.format(db=db),
               epilog=EXITS + " 3 (doctor only) something would stop the commands.\n\nRun `cli.py <command> --help` for each command's arguments, output and failures.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>", parser_class=Parser)

    d = sub.add_parser("doctor", formatter_class=Formatter, help="check the token file, the token and the archive",
                       description="Check that the token file exists, is mode 600 and ignored by git, that Discord accepts the token (one request; prints only the account's name), and that the archive is one this version writes.",
                       epilog="Output: one JSON line: ok, blockers [{problem, fix}], warnings, token_file, token_source, account, archive {path, bytes}, results {path, files, bytes} (the result files of this working directory).\n\n"
                              "Exit: 0 ok; 3 at least one blocker, each with its fix; 2 bad arguments.")
    d.set_defaults(run=run_doctor)

    s = sub.add_parser("servers", formatter_class=Formatter, help="list your servers and DMs",
                       description="List the servers you are in (id, name, member count) and your DMs (id, the other people's names, last activity).",
                       epilog="Output: compact text, one line each.\n\n" + EXITS)
    s.set_defaults(run=run_servers)

    c = sub.add_parser("channels", formatter_class=Formatter, help="list a server's channels you can see",
                       description="List the channels of a server you can see, as a tree under their categories: kind, name, id, when the last message was sent, and whether you can read its history. Channels hidden from you are not listed, only counted. Threads and forum posts are not channels here: use `threads`.",
                       epilog="Output: compact text.\n\n" + EXITS)
    c.add_argument("server", help=f"the server: {TARGET_FORMS}. A name that matches several servers is refused with the candidates.")
    c.set_defaults(run=run_channels)

    t = sub.add_parser("threads", formatter_class=Formatter, help="list a channel's threads or a forum's posts",
                       description="List the threads of a text channel, or the posts of a forum, active and archived together, most recent activity first: name, id, message count, last activity, archived or not, and for a forum the start of each post. Each thread is its own channel: read it with `read <thread id>`.",
                       epilog="Output: compact text, ending with how many there are in total when Discord says so.\n\n" + EXITS)
    t.add_argument("channel", help=f"the channel or forum: {TARGET_FORMS}.")
    t.add_argument("--limit", type=int, default=50, help="list at most this many (default 50; Discord pages them 25 at a time).")
    t.set_defaults(run=run_threads)

    r = sub.add_parser("read", formatter_class=Formatter, help="fetch a channel's or thread's messages into the archive and print a transcript",
                       description="Fetch messages of one channel, thread or DM from Discord, store them in the archive, and print a transcript. A range is always fetched fresh, so edits, reactions and deletions since an earlier read show up; `--skip-covered` instead skips ranges the archive already read through. A parent channel's read does not include its threads.\n\n"
                                   "Choose the range with one of: --last N (the newest N), --after X (oldest first from X, optionally up to --before Y), --before Y alone (newest first, going back from Y), --around M (one window around a message), --new (from where the archive last reached the newest message, up to now). With none of these, --last is used with --limit.",
                       epilog="Output: a header -- target (server › channel), range, count, whether the range was cut and the arguments that continue it, and the result file holding the whole transcript (a new file under ./.discord/results/ in the working directory, never overwritten) -- then the transcript itself when it has at most --inline messages.\n\n"
                              "Transcript: a line `── <date> ──` starts each local day. Each message is `HH:MM <author>: <text>  [<message id>]`, times local. `(edited)` and `(pinned)` follow the author. A reply shows `(reply to <author>: \"<start of its text>\")`, `(reply to a deleted message)`, or `(reply to [<id>])` when Discord did not send the target along (search hits). Mentions are written @name, #channel and @role, custom emoji :name:, and Discord timestamps as local times; an id that could not be named stays as @unknown(<id>). Lines starting with `  + ` belong to the message above: file, embed, sticker, poll, reactions, thread (a thread started from the message, with its [id]). A forward says where it came from; Discord does not give the forwarded message's original author. `* ` marks an event Discord wrote (joins, pins, calls, threads created, poll results); `[type N]` one this version does not know. The first entry of a thread made from a message shows that message (it lives in the parent channel).\n\n"
                              + EXITS + "\n\nA forum is refused (read its posts, listed by `threads`); so is a channel whose history you cannot read.")
    r.add_argument("channel", help=f"the channel, thread or DM: {TARGET_FORMS} (a name is looked up among channels `channels` and `servers` listed; server/channel also works).")
    r.add_argument("--last", type=int, metavar="N", help="the newest N messages.")
    r.add_argument("--after", metavar="X", help=f"messages after X, oldest first. X is {TIME_FORMS}.")
    r.add_argument("--before", metavar="Y", help="messages before Y. With --after: stop at Y. Alone: newest first going back from Y. Same forms as --after.")
    r.add_argument("--around", metavar="M", help="one window of up to --limit (at most 100) messages around message M (an id or link).")
    r.add_argument("--new", action="store_true", help="from where this archive last reached the channel's newest message, up to now. Refused when it never has: start with --last N.")
    r.add_argument("--limit", type=int, default=200, help="fetch at most this many messages (default 200). When it cuts the range, the header gives the arguments that continue it.")
    r.add_argument("--skip-covered", action="store_true", help="skip ranges the archive already read through instead of fetching them again; their stored messages still appear in the transcript. Rerunning an interrupted long read with this continues it.")
    r.add_argument("--quiet", action="store_true", help="print the header only and skip looking up members' server nicknames (for bulk collection).")
    r.add_argument("--inline", type=int, default=200, metavar="N", help="print the transcript after the header only when it has at most N messages (default 200); the result file always has all of it.")
    r.set_defaults(run=run_read)

    q = sub.add_parser("search", formatter_class=Formatter, help="Discord's own search over a server or a DM",
                       description="Search a server or a DM with Discord's own index, newest first by default. Discord matches whole words, case-insensitively: a Korean word with a particle attached (스터디를) is indexed as a different word from 스터디, and a part of a word matches nothing. Hits come without the messages around them; read those with `read <channel> --around <id>`. Hits are stored in the archive as they are, but do not count as read-through ranges.",
                       epilog="Output: a header -- target, filters, Discord's estimated total, which hits this page holds, the --offset for the next page, the result file -- then the hits as a transcript (each line names its channel) when there are at most --inline.\n\n"
                              "The total is an estimate and can be off by a few. Discord stops paging at an offset of about 10,000.\n\n" + EXITS)
    q.add_argument("target", help=f"the server or DM: {TARGET_FORMS}.")
    q.add_argument("--content", help="words that must all appear (whole words).")
    q.add_argument("--from", dest="authors", action="append", metavar="USER", help="only messages by this user: an id, <@id>, or a name that matches exactly one person the archive has seen in this server. Repeatable.")
    q.add_argument("--mentions", action="append", metavar="USER", help="only messages mentioning this user (same forms as --from). Repeatable.")
    q.add_argument("--in", dest="channels", action="append", metavar="CHANNEL", help="only in this channel or thread (an id, link, or a name in this server); a channel includes the messages of its threads. Repeatable.")
    q.add_argument("--has", action="append", choices=("link", "embed", "file", "image", "video", "sound", "sticker", "poll", "snapshot"),
                   help="only messages that have this (snapshot: a forwarded message). Repeatable; all must hold.")
    q.add_argument("--after", metavar="X", help=f"only messages after X: {TIME_FORMS}.")
    q.add_argument("--before", metavar="Y", help="only messages before Y (same forms).")
    q.add_argument("--pinned", action="store_true", help="only pinned messages.")
    q.add_argument("--sort", choices=("newest", "oldest", "relevance"), default="newest", help="order of the hits (default newest).")
    q.add_argument("--offset", type=int, default=0, help="skip this many hits (paging; at most 9975).")
    q.add_argument("--limit", type=int, default=25, help="hits to fetch, 25 per request (default 25).")
    q.add_argument("--count", action="store_true", help="print only Discord's estimated total (one request), for sizing a read before making it.")
    q.add_argument("--inline", type=int, default=200, metavar="N", help="print the hits after the header only when there are at most N (default 200).")
    q.set_defaults(run=run_search)

    g = sub.add_parser("get", formatter_class=Formatter, help="GET any Discord API path",
                       description="GET any path of Discord's API (v9) as the user, for reads the other commands do not cover: pins (channels/<id>/messages/pins), your recent mentions across servers (users/@me/mentions), a server's details, and so on. Only GET exists here; nothing can be sent, changed or deleted.",
                       epilog="Output: a header with the path and the result file, then the JSON; with --transcript, the messages found in the response as a transcript (see `read --help` for its notation), stored in the archive as stray rows.\n\n" + EXITS)
    g.add_argument("path", help="the path after /api/v9/, such as users/@me/mentions or channels/123/messages/pins.")
    g.add_argument("--query", action="append", default=[], metavar="K=V", help="a query parameter; repeatable, and a key may repeat.")
    g.add_argument("--transcript", action="store_true", help="print the message objects in the response as a transcript instead of JSON.")
    g.add_argument("--inline", type=int, default=200, metavar="N", help="print the body after the header only when it has at most N messages, or for JSON at most N×1000 characters (default 200).")
    g.set_defaults(run=run_get)

    w = sub.add_parser("show", formatter_class=Formatter, help="print messages the archive already holds, by id, without a request",
                       description="Print messages the archive already holds, chosen by id, as a transcript. No request is made and no token is read, so it works when Discord cannot be reached. "
                                   "Choosing which messages to show is SQL over the archive; this command only prints them, for example:\n\n"
                                   f"sqlite3 -readonly \"{db}\" \"SELECT id FROM messages WHERE channel_id = <id> AND sent_at >= '2026-11-01' ORDER BY id\" | uv run \"{SKILL_DIR}/scripts/cli.py\" show -",
                       epilog="Output: a header -- how many messages in how many channels and that no request was made; the earliest and latest time these rows were fetched (each message is as it was when fetched, so edits, reactions and deletions since may be missing); how many lie outside every read-through range (search hits and reply targets, stored one by one); the ids not in the archive; the result file holding the whole transcript (a new file under ./.discord/results/) -- then the transcript itself when it has at most --inline messages.\n\n"
                              "Transcript: grouped by channel, groups in the order of their first message, messages oldest first. Each group starts with `══ <server › #channel> (<kind>) [<channel id>] · N messages ══` and `read through: <from> → <to>; …`, the archive's read-through ranges of that channel that overlap the span from the group's first message to its last, in local time, or `read through: nowhere here` when there is none (those rows came from search or as reply targets). This command does not know which period you meant: compare it with these ranges, since outside them the archive may lack messages. "
                              "Between two messages printed one after the other, `⋯ N not shown` means the archive holds N messages between them that were not asked for, and `⋯ not read through` means no read went through the whole stretch between them, so messages there may be missing from the archive; both can share one line, and no line means the two are neighbours inside a read-through range. "
                              "`(gone)` after a message's [id]: a later read of its range did not find it (deleted, or no longer visible to you); it is not proof of deletion, and the row shows the last version seen. On a thread's first entry it belongs to that entry, not to the original message in the parent channel. Everything else is the notation of `read --help`.\n\n"
                              "Exit: 0 done; 1 refused (stdout is one JSON line: error, message, fix, help): there is no archive yet, or none of the ids is in it; 2 the command line must change (same JSON line): something among the ids is neither a message id nor a message link, so a mistake in the SQL is never dropped silently.")
    w.add_argument("ids", nargs="+", metavar="ID", help="message ids or discord.com/channels/… message links, any number; `-` reads ids separated by whitespace from stdin, such as the output of an sqlite3 query.")
    w.add_argument("--inline", type=int, default=200, metavar="N", help="print the transcript after the header only when it has at most N messages (default 200); the result file always has all of it.")
    w.set_defaults(run=run_show)
    return p


# -- commands ------------------------------------------------------------------------------------


def run_doctor(args) -> int:
    report = doctor.check(DATA_DIR, OUTPUT_DIR)
    print(json.dumps(report, ensure_ascii=False))
    return EXIT_OK if report["ok"] else EXIT_BLOCKED


class _Session:
    """The token, the API and the archive, opened for one command and closed after it."""

    def __enter__(self):
        token, _ = credentials.load()
        self.api = discord_api.connect(token, token_hint=credentials.hint())
        self.db = archive.Archive(DATA_DIR / "discord.db")
        return self

    def __exit__(self, *exc) -> None:
        self.db.close()


RESULTS = OUTPUT_DIR


def run_servers(args) -> int:
    with _Session() as s:
        print(directory.servers_text(s.api, s.db))
    return EXIT_OK


def run_channels(args) -> int:
    with _Session() as s:
        print(directory.channels_text(s.api, s.db, args.server))
    return EXIT_OK


def run_threads(args) -> int:
    if args.limit < 1:
        raise Refusal("--limit must be at least 1", kind="usage")
    with _Session() as s:
        print(directory.threads_text(s.api, s.db, args.channel, limit=args.limit))
    return EXIT_OK


def run_read(args) -> int:
    with _Session() as s:
        print(reading.read(s.api, s.db, RESULTS, args.channel, last=args.last, after=args.after, before=args.before,
                           around=args.around, new=args.new, limit=args.limit, skip_covered=args.skip_covered,
                           quiet=args.quiet, inline=args.inline))
    return EXIT_OK


def run_search(args) -> int:
    with _Session() as s:
        print(reading.search(s.api, s.db, RESULTS, args.target, content=args.content, authors=args.authors or [],
                             mentions=args.mentions or [], channels=args.channels or [], has=args.has or [],
                             after=args.after, before=args.before, pinned=args.pinned, sort=args.sort,
                             offset=args.offset, limit=args.limit, count=args.count, inline=args.inline))
    return EXIT_OK


def run_get(args) -> int:
    query = []
    for item in args.query:
        k, sep, v = item.partition("=")
        if not sep or not k:
            raise Refusal(f"--query {item!r} is not K=V", kind="usage")
        query.append((k, v))
    with _Session() as s:
        print(reading.get(s.api, s.db, RESULTS, args.path, query, transcript_view=args.transcript, inline=args.inline))
    return EXIT_OK


_SNOWFLAKE = re.compile(r"\d{15,20}")


def _message_ids(values: list[str]) -> list[int]:
    """The message ids in `values` (ids, message links, or `-` for whitespace-separated ones on stdin). Anything else refuses the whole list."""
    words: list[str] = []
    for v in values:
        words += sys.stdin.read().split() if v == "-" else [v]
    ids, bad = [], []
    for word in words:
        if _SNOWFLAKE.fullmatch(word):
            ids.append(int(word))
            continue
        link = discord_api.parse_link(word)
        if link and link[2]:
            ids.append(link[2])
        else:
            bad.append(word)
    if bad:
        shown = ", ".join(repr(b[:80]) for b in bad[:5]) + (f" and {len(bad) - 5} more" if len(bad) > 5 else "")
        raise Refusal(f"{len(bad)} of the {len(words)} values given are neither a message id nor a message link: {shown}",
                      fix="select only the id column (SELECT id FROM …), or give ids or discord.com/channels/… message links", kind="usage")
    if not ids:
        raise Refusal("no message ids were given (stdin was empty)", fix="check that the query returns rows", kind="usage")
    return ids


def run_show(args) -> int:
    ids = _message_ids(args.ids)
    db = archive.Archive.existing(DATA_DIR / "discord.db")
    try:
        print(reading.show(db, RESULTS, ids, inline=args.inline))
    finally:
        db.close()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except Refusal as e:
        out = {"error": e.kind, "message": e.message}
        if e.fix:
            out["fix"] = e.fix
        out["help"] = f"cli.py {args.command} --help"
        print(json.dumps(out, ensure_ascii=False))
        return EXIT_USAGE if e.kind == "usage" else EXIT_REFUSED
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted", "message": "stopped; pages already fetched are stored, and `read --skip-covered` continues"}))
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
