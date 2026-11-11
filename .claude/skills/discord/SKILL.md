---
name: discord
description: Read Discord as the user, read-only, through their own token: list servers, channels, threads and DMs, read a channel's or thread's messages into a local SQLite archive and a transcript, use Discord's own search, and query the archive with SQL. Use when the user asks what was said on Discord, wants to catch up on a channel, find a past discussion, collect someone's messages, or analyse a server's history. Triggers include discord, 디스코드, 디코, 채널 읽어, 디스코드에서 찾아, 서버 대화, 스레드, DM. Not for sending, reacting or any change on Discord, for bots or the Discord developer API, or for Discord's own documentation.
allowed-tools: Bash(uv run "${CLAUDE_SKILL_DIR}/scripts/cli.py" *), Bash(sqlite3 -readonly "${CLAUDE_SKILL_DIR}/data/discord.db" *)
---

# Reading Discord as the user

Every command goes through one CLI, and the archive is queried read-only; both are pre-approved only as spelled here, so a path kept in a shell variable or written another way stops at a prompt.

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/cli.py" --help
sqlite3 -readonly "${CLAUDE_SKILL_DIR}/data/discord.db" ".schema"
```

Each command's `--help` owns its arguments, output and exits; the schema's comments say what each table means and what it cannot show.

## Threads are channels

A parent channel's read does not cover its threads or a forum's posts; a claim about what was said in a channel, threads included, needs evidence from both, and `threads` lists them. Discord's search filtered to a channel does include its threads, so its estimated count sizes the channel and its threads together.

## What it costs

A broad read takes many requests. Automating a user token is against Discord's terms, so every request is pressure on the user's own account. The archive and the result files keep other people's messages, DMs included: being able to read them is not leave to pass them on.

## What you cannot see from here

- The user's unread state does not exist here.
- What attachments contain, and anything said in voice.
