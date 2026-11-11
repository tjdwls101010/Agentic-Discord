"""Reading Discord as the user: the package behind `scripts/cli.py`.

Units, by kind (imports run features -> systems and stores -> shared, never back):

- `reading` (feature): collecting a channel's messages into the archive, server-side search, raw GET.
- `directory` (feature): servers, the channel tree and what you can see of it, threads, people's names.
- `doctor` (feature): whether the token file, the token and the archive are usable.
- `discord_api` (system): Discord's REST API -- requests, retries, snowflakes and times, permission bits, and every raw field turned into the skill's own terms.
- `archive` (store): the SQLite archive (`schema.sql`), which ranges were read through, and the per-call result files.
- `credentials` (store): reading the token from `scripts/.env`.
- `records` (shared): the skill's own terms for messages, users and channels.
- `transcript` (shared): messages whose names are resolved -> transcript text.
- `errors` (shared): `Refusal`, the one way a unit says it will not or cannot do what was asked.
"""

__version__ = "0.0.0"
