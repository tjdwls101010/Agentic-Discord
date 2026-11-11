PRAGMA user_version = 1;

CREATE TABLE servers (
  -- The API, and every `raw` column, call a server a guild (guild_id).
  -- Conventions for the whole archive: every id is a Discord snowflake stored as INTEGER, and
  -- ((id >> 22) + 1420070400000) is its creation time in ms since the Unix epoch; every *_at is
  -- UTC ISO-8601 text ending in Z; every is_* and can_* is 0 or 1. Only the skill's commands
  -- write here; nothing is a history: each row is the latest fetch of that thing.
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  fetched_at  TEXT NOT NULL
) STRICT;

CREATE TABLE channels (
  -- Everything that holds messages or groups what does: channels, categories, threads, forum posts, DMs.
  -- A thread or forum post is its own channel: its messages carry channel_id = the thread's id,
  -- never the parent's, so a parent channel's rows say nothing about its threads, and listing a
  -- server's channels does not list its threads.
  id                INTEGER PRIMARY KEY,
  server_id         INTEGER REFERENCES servers(id),   -- NULL for kind dm, group_dm and unknown
  parent_id         INTEGER REFERENCES channels(id),  -- a channel's category; a thread's or forum post's channel
  kind              TEXT NOT NULL CHECK (kind IN ('text','announcement','forum','voice','stage','category',
                                                  'thread','private_thread','dm','group_dm','other','unknown')),
    -- other: a type Discord reported that this version has no word for
    -- unknown: a message pointed at this channel but it was never listed, so its name, server and kind are not known
  name              TEXT,     -- a DM is named after the other people in it. NULL: not known
  topic             TEXT,
  is_archived       INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0,1)),  -- threads and forum posts only
  can_view          INTEGER CHECK (can_view IN (0,1)),          -- worked out from your roles at fetched_at, not from a request. 0: listed so a mention of it can be named; its messages cannot be read. NULL: not worked out (threads, DMs, unknown)
  can_read_history  INTEGER CHECK (can_read_history IN (0,1)),  -- same working-out and NULL. 0: visible, but Discord returns none of its messages
  last_message_id   INTEGER,  -- the newest message Discord reported at fetched_at; it may since have been deleted
  fetched_at        TEXT NOT NULL
) STRICT;

CREATE TABLE roles (
  -- Roles seen when a server was listed, kept to name a role mention (<@&id>). Not kept current.
  id         INTEGER PRIMARY KEY,
  server_id  INTEGER NOT NULL REFERENCES servers(id),
  name       TEXT NOT NULL
) STRICT;

CREATE TABLE users (
  -- People, bots and webhooks that appeared in something fetched: authors, mentions, reply targets.
  -- Not a member list. Discord gives this account no way to list or search a server's members,
  -- so someone who never appeared in a fetched message is not here.
  id           INTEGER PRIMARY KEY,
  username     TEXT NOT NULL,   -- the handle Discord reports; a webhook's is whatever it posted as, so it is not unique
  global_name  TEXT,            -- the account-wide display name, when one is set
  is_bot       INTEGER NOT NULL CHECK (is_bot IN (0,1)),  -- bots and webhooks
  fetched_at   TEXT NOT NULL
) STRICT;

CREATE TABLE members (
  -- A user as one server shows them, as of fetched_at. The name Discord displays there is nick,
  -- else users.global_name, else users.username.
  -- No row: never looked up, or the lookup failed (lookups happen when a transcript naming that user is printed).
  server_id   INTEGER NOT NULL REFERENCES servers(id),
  user_id     INTEGER NOT NULL REFERENCES users(id),
  nick        TEXT,             -- NULL: no nickname in this server
  is_member   INTEGER NOT NULL CHECK (is_member IN (0,1)),  -- 0: Discord said the user is not in the server (left it, or a webhook)
  fetched_at  TEXT NOT NULL,
  PRIMARY KEY (server_id, user_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE messages (
  -- The latest fetched version of each message. This is not the channel: a row missing here may
  -- never have been fetched, so a count is a count of stored rows. `coverage` says which id ranges
  -- were read through; rows with absent_at set are still counted unless excluded.
  id           INTEGER PRIMARY KEY,
  channel_id   INTEGER NOT NULL REFERENCES channels(id),
  author_id    INTEGER NOT NULL REFERENCES users(id),
  sent_at      TEXT NOT NULL,
  edited_at    TEXT,            -- Discord's last-edit time as of fetched_at; earlier versions are not kept
  kind         TEXT NOT NULL CHECK (kind IN ('message','reply','thread_starter','system','other')),
    -- message: someone's post, an app's answer to a command included
    -- reply: a post answering reply_to_id
    -- thread_starter: the empty first row of a thread made from a message; that message is in the parent channel, with id = this row's channel_id
    -- system: an event Discord wrote (join, pin, boost, thread created, call, poll result); json_extract(raw,'$.type') says which
    -- other: a type this version does not know; read raw
  content      TEXT NOT NULL,   -- Discord's top-level text only. A mention is <@user_id>, <#channel_id> or <@&role_id> and a custom emoji is <:name:id>, so a name is not matched by LIKE. Text inside embeds, forwards and polls is only in raw, and content is empty when a post is nothing else. LIKE matches substrings, where Discord's own search matches whole words
  reply_to_id  INTEGER,         -- kind = 'reply' only. Not a foreign key: the target may be unfetched or deleted, and an inner join to it drops the reply
  raw          TEXT NOT NULL CHECK (json_valid(raw)),  -- the message object from the API at fetched_at. Holds what has no column: attachments[] (url is signed and expires), embeds[], reactions[] (count as of fetched_at), mentions[], pinned, poll, sticker_items[], message_snapshots[] (a forward's content; its original author is not given), referenced_message, interaction_metadata
  fetched_at   TEXT NOT NULL,   -- the last time a fetch returned this message, not when anything changed: re-reading an unchanged message moves it
  absent_at    TEXT             -- when a fetch that read through this id's range first found the message missing: deleted, or no longer visible to you. Not when that happened. The row keeps the last version seen. NULL: no fetch has found it missing
) STRICT;
CREATE INDEX messages_by_channel ON messages (channel_id, id);
CREATE INDEX messages_by_author  ON messages (author_id, id);

CREATE TABLE coverage (
  -- Each row: channel_id was read through from lo_id to hi_id (inclusive) with no gap, so every
  -- message found there is in `messages`. Not a snapshot: its pieces were fetched at different
  -- times, and a message deleted before its page was fetched was never seen.
  -- Outside these ranges the archive holds only stray rows (search hits, reply targets), and a row's absence proves nothing.
  -- Bounds are positions in id space, not ids of messages. One channel's rows never overlap or touch (enforced).
  channel_id         INTEGER NOT NULL REFERENCES channels(id),
  lo_id              INTEGER NOT NULL,
  hi_id              INTEGER NOT NULL,
  oldest_fetched_at  TEXT NOT NULL,  -- the earliest fetch among the pieces merged into this range: all of it was read at or after this time
  head_at            TEXT,           -- when this range was seen to reach the channel's newest message (nothing newer existed then); `read --new` continues from hi_id of the row that has it. NULL: this range did not reach the newest message. At most one per channel (enforced)
  PRIMARY KEY (channel_id, lo_id),
  CHECK (lo_id <= hi_id)
) STRICT, WITHOUT ROWID;
CREATE UNIQUE INDEX coverage_one_head ON coverage (channel_id) WHERE head_at IS NOT NULL;
CREATE TRIGGER coverage_separate_on_insert BEFORE INSERT ON coverage
  WHEN EXISTS (SELECT 1 FROM coverage c WHERE c.channel_id = NEW.channel_id
               AND NEW.lo_id <= c.hi_id + 1 AND c.lo_id <= NEW.hi_id + 1)
  BEGIN SELECT RAISE(ABORT, 'coverage ranges of one channel overlap or touch: merge them into one row'); END;
CREATE TRIGGER coverage_separate_on_update BEFORE UPDATE OF channel_id, lo_id, hi_id ON coverage
  WHEN EXISTS (SELECT 1 FROM coverage c WHERE c.channel_id = NEW.channel_id
               AND NOT (c.channel_id = OLD.channel_id AND c.lo_id = OLD.lo_id)
               AND NEW.lo_id <= c.hi_id + 1 AND c.lo_id <= NEW.hi_id + 1)
  BEGIN SELECT RAISE(ABORT, 'coverage ranges of one channel overlap or touch: merge them into one row'); END;

CREATE VIEW messages_named AS
  -- messages with names attached. author is the cached display name (nick, else global_name, else
  -- username): it can be stale, and two people can share one, so group by author_id. channel,
  -- parent and server can be NULL. raw is left out; join messages by id for it.
  SELECT m.id, m.sent_at, m.edited_at, m.kind, m.content, m.reply_to_id, m.fetched_at, m.absent_at,
         m.channel_id, c.name AS channel, p.name AS parent,   -- parent: a thread's channel, or a channel's category
         c.server_id, s.name AS server,
         m.author_id, COALESCE(mb.nick, u.global_name, u.username) AS author, u.is_bot
    FROM messages m
    JOIN channels c ON c.id = m.channel_id
    JOIN users    u ON u.id = m.author_id
    LEFT JOIN channels p  ON p.id = c.parent_id
    LEFT JOIN servers  s  ON s.id = c.server_id
    LEFT JOIN members  mb ON mb.server_id = c.server_id AND mb.user_id = m.author_id;

CREATE VIEW coverage_named AS
  -- coverage with its bounds as times. A time is the millisecond a bound falls in, and ids within
  -- one millisecond share it, so compare ids, not these times, to test whether a message is covered.
  SELECT cv.channel_id, c.name AS channel, cv.lo_id, cv.hi_id,
         strftime('%Y-%m-%dT%H:%M:%fZ', ((cv.lo_id >> 22) + 1420070400000) / 1000.0, 'unixepoch') AS from_at,
         strftime('%Y-%m-%dT%H:%M:%fZ', ((cv.hi_id >> 22) + 1420070400000) / 1000.0, 'unixepoch') AS to_at,
         cv.oldest_fetched_at, cv.head_at
    FROM coverage cv JOIN channels c ON c.id = cv.channel_id;
