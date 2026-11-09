# CLI reference

Flags, formats, paging, caching and exit codes. Commands below are run from the
skill directory (`uv run scripts/tweetcider.py ...`).

## Contents

- [Targets](#targets)
- [Output](#output)
- [Paging](#paging)
- [Exit codes](#exit-codes)
- [API behaviour worth knowing](#api-behaviour-worth-knowing)
- [Cache and concurrency](#cache-and-concurrency)
- [Environment](#environment)

## Targets

`status`, `thread`, `conversation`, `quotes`, `download` take a bare snowflake
id or any x.com / twitter.com / fxtwitter.com / fixupx.com / vxtwitter.com
permalink. `statuses` takes `handle`, `@handle`, a profile URL, or `id:<n>` for
numeric handles.

## Output

`--format` / `-f`:

| Value | Use |
| --- | --- |
| `summary` (default) | Compact text: one header line per post (author, date, counts), `\|` text, URL last. Replies are indented under the reply they answer (`↳`). Best for reading and context budget. |
| `json` | Tidied JSON (below). Add `--compact` for one line. Has `lang` and exact counts. |
| `jsonl` | One tidied object per status, one per line. |
| `md` | Paste-ready: `>` quoted bodies, quoted posts as `>>`, attribution line under each (the `citing.md` shape). |
| `urls` | One canonical post URL per line. |
| `media-urls` | One media URL per line. |

Modifiers, valid with any format:

- `--select status.text --select status.author.screen_name` projects dotted
  paths; `results[].id` walks a list. Output becomes JSON.
- `--full-text` disables the 600-char clip; `--text-limit N` changes it.
- `--out FILE` / `-o FILE` writes to a file instead of stdout.
- `--raw` preserves the API response fields without tidying.
- `--lang xx` asks for a translation; `--about-account` adds account-origin info.

Tidied JSON (`json`, `jsonl`, `--select`) drops nulls, empty objects and
formatting-only fields (`raw_text`, `embed_card`, `provider`, `source`, ...),
gives dates as ISO 8601 (`2026-11-06T22:50:41Z`), and slims each post's `author`
to `id`, `screen_name`, `name`, `followers`, `following`, `statuses`, `joined`,
`verified`. A post quoted again later in the same output becomes
`{"id", "url", "shown_above": true}`. Use `--raw` for the full API body.

Text is clipped at 600 chars (only when at least 100 more remain); the clip
marker says `--full-text`. Summary and md drop the `@mentions` X puts at the
start of a reply; `json` keeps `text` verbatim.

## Paging

- `--pages N` follows `cursor.bottom` for N pages (default 1).
- `--all` follows the cursor until it runs out (hard cap 1000 pages). Expensive
  on big accounts; prefer `--max-items` or `--pages`.
- `--max-items N` truncates the merged list. If it cuts through a page, no next
  cursor is returned (it would skip the rest of that page).
- `--count N` sets the API page size.
- `--cursor C` resumes from `cursor.bottom` (`-f json --select cursor.bottom`).
  When more pages exist, summary and md end with a `more:` line saying what to run.
- `conversation --ranking-mode likes|recency` (default `likes`).
- `statuses --since <unix-seconds>`, `--with-replies`, `--group-threads`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | OK |
| 1 | Local/runtime failure (disk, config) |
| 2 | Bad usage, bad URL or handle, failed validation |
| 3 | API returned an error (`code` >= 400, network failure) |
| 4 | No content (nothing newer than `--since`). Success, not failure. |
| 5 | `fetch` or `download` had one or more failures |

Branch on the code, not on stderr text.

## API behaviour worth knowing

- A `tombstone` object is a deleted, private or withheld post. Report it as
  unavailable; it is not an error.
- `code` in the JSON body mirrors the HTTP status. A 200 response with
  `code: 404` is a real failure.
- `search` and `quotes` are unreliable on the public instance, and can return
  HTTP 404 for every query, including posts that have quotes. The CLI retries
  empty results (`--retry-empty N`, default 2). Retry once yourself, then report
  the failure; do not report "no results" for a 404.
- When `quotes` still fails after a retry, give the user the count
  (`status <url> --format json --select status.quotes`) and ask for specific
  quote-tweet links; `status` reads those reliably.
- `statuses` includes reposts. A post with `reposted_by` was written by someone
  else; attribute it to its own author, not the account you queried.
- `conversation` returns a sample of replies, not all. Say "top replies", and
  use `--pages N` for more; the header says `replies · 40 of 74`.
- In summary, `quoting @x` introduces the post *this* one quotes, the opposite
  direction from the `quotes` command. A post already printed is not repeated:
  it shows as `quoting @x, shown above`.
- Every request needs a `User-Agent`; the CLI sends one. A bare `curl` gets 401.
- Rate limit is 1000 requests/minute per IP.

## Cache and concurrency

Responses are cached in SQLite under `$TWEETCIDER_CACHE_DIR` (default
`~/.cache/tweetcider`) for 300 seconds. `--no-cache` skips it, `--cache-ttl 0`
disables it. Only successful, nonempty responses are stored.

`fetch` runs up to `--concurrency` requests at once (default 4) and prints in
input order. `json` emits an array, `jsonl` one object per line. In JSONL, a
failed input is an object with `input` and `error` fields.

## Environment

| Variable | Effect |
| --- | --- |
| `TWEETCIDER_API_BASE` | Alternate base URL (`https://api.fxbluesky.com` for Bluesky). |
| `TWEETCIDER_USER_AGENT` | Override the User-Agent. |
| `TWEETCIDER_TIMEOUT` | Request timeout in seconds (default 30). |
| `TWEETCIDER_CACHE_TTL` | Cache lifetime in seconds. |
| `TWEETCIDER_CACHE_DIR` | Where `cache.db` lives. |

Self-hosted FxEmbed works with `--base-url http://localhost:3000`. `mine` settings
are in `mine.md`.
