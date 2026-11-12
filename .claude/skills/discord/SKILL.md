---
name: discord
description: Read Discord as the user, read-only, through their own token: list servers, channels, threads and DMs, read a channel's or thread's messages into a local SQLite archive and a transcript, use Discord's own search, show what the archive already holds without a request, download and view the files and images attached to messages, and query the archive with SQL. Use when the user asks what was said on Discord, wants to catch up on a channel, find a past discussion, collect someone's messages, analyse a server's history, or needs to see an image or file posted on Discord. Triggers include discord, 디스코드, 디코, 채널 읽어, 디스코드에서 찾아, 서버 대화, 스레드, DM, 디스코드 첨부, 디스코드 이미지. Not for sending, reacting or any change on Discord, for bots or the Discord developer API, or for Discord's own documentation.
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

What the archive already holds costs no request to read again. Before reading Discord, check what it holds for that channel (`coverage_named`), and request only what lies outside it or what needs updating.

Automating a user token is against Discord's terms, and Discord enforces it: users of tools that only read, like this one, report being logged out and made to reset their password, with suspension further along, and that slowing down did not prevent it. Every request is pressure on the user's own account.

Nothing here sends, and sending with this token by any route puts the account at risk: after one message was sent with it outside the skill, Discord recorded a spam violation and required a password reset, and the token answered 401 from about 20 seconds later.

The archive, the result files and downloaded files hold other people's messages and uploads, DMs included: being able to read them is not leave to disclose them to others.

## What you cannot see from here

- The user's unread state does not exist here.
- Link-preview images (`files` takes attachments only; a preview's URL stays in `raw`), and anything said in voice.
