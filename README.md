<img src="assets/icon.svg" width="64" height="64" alt="">

# TweetCider

Read X/Twitter posts, threads, timelines and media through the FxTwitter JSON
API. No API key, no login, public data only.

One Python file. Three dependencies. It is a CLI and an agent skill, not a
library.

```bash
uv run skills/tweetcider/scripts/tweetcider.py status https://x.com/jack/status/20
```

```
@jack · 2006-03-21 20:50 UTC · 325k likes · 19k replies · 126k reposts · 8k quotes
  | just setting up my twttr
  https://x.com/jack/status/20
```

## Requirements

- Python 3.14 or newer
- [uv](https://docs.astral.sh/uv/)

`uv run` reads the PEP 723 header and installs `niquests`, `cyclopts` and `tokenizers` on
first use. Nothing else to set up.

## Install the agent skill

Run from your project:

```bash
bunx skills add jwinpbe/tweetcider --skill tweetcider
```

Choose your agent when prompted. Add `-g` to install for your user instead of one project,
or `-a codex` / `-a claude-code` to select an agent directly. The skill runs
with Python 3.14+ and `uv`, as described above.

The [skills CLI](https://www.skills.sh/docs/cli) uses `add` to install from a
repository; `install` restores skills from a project's lockfile.

## Commands

| Command | What it returns |
| --- | --- |
| `status <id-or-url>` | One post: text, stats, media, quote, poll |
| `thread <id-or-url>` | The author's self-reply chain |
| `conversation <id-or-url>` | The thread plus other people's replies |
| `quotes <id-or-url>` | Posts quoting it |
| `mine <id-or-url> <query>` | The post, plus the replies, quotes and thread posts relevant to the query, ranked by a decision model |
| `statuses <handle>` | Recent posts (`--since`, `--with-replies`, `--group-threads`) |
| `search <query>` | Post search (X operators work) |
| `typeahead <text>` | Autocomplete users, topics, events |
| `trends` | Trending topics |
| `get <path>` | Any API path, `--param=k=v` |
| `fetch <url>...` | Several posts at once, concurrently |
| `download <target>` | Save media to disk |

`status`, `thread`, `conversation`, `quotes`, `mine` and `download` accept a
bare snowflake id or any x.com / twitter.com / fxtwitter.com /
fixupx.com / vxtwitter.com permalink.

## Output

```bash
tweetcider status 20 --format json          # parse me
tweetcider status 20 -f md --full-text      # paste me
tweetcider thread 20 -f urls                # one post URL per line
tweetcider status 20 --select status.text   # one field, as JSON
tweetcider status 20 -o post.txt            # write to a file
```

Formats: `summary` (default), `json`, `jsonl`, `md`, `urls`, `media-urls`.

## Common flags

```
Output   --format/-f  --raw  --compact  --select  --text-limit  --full-text  --out/-o
         --media-kind  --best-variant/--no-best-variant
Network  --base-url  --user-agent  --timeout  --retries  --verbose/-v
Paging   --pages  --all  --max-items  --retry-empty  --count  --cursor
Query    --lang/-l  --about-account
Cache    --cache/--no-cache  --cache-ttl  --concurrency
```

`--help` on any command shows the same list, grouped into panels.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | OK |
| 1 | Local/runtime failure |
| 2 | Bad usage, bad URL, failed validation |
| 3 | API error, including a `200` body with `code: 404` |
| 4 | No content (nothing newer than `--since`) |
| 5 | `fetch` or `download` had partial failures |

Agents can branch on the code instead of parsing stderr.

## Environment

| Variable | Default |
| --- | --- |
| `TWEETCIDER_API_BASE` | `https://api.fxtwitter.com` |
| `TWEETCIDER_USER_AGENT` | `tweetcider/1.0.0` |
| `TWEETCIDER_TIMEOUT` | `30` |
| `TWEETCIDER_CACHE_TTL` | `300` |
| `TWEETCIDER_CACHE_DIR` | `~/.cache/tweetcider` |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` | none; `mine` only (Workers AI token) |
| `TWEETCIDER_CF_KEY_FILE` | none; two-line file: account id, then token |
| `TWEETCIDER_DECIDER_URL` | none; local clef-flash `/v1/systemone` for `mine`, no credentials |

Self-hosted FxEmbed works with `--base-url`. FxBluesky is
`--base-url https://api.fxbluesky.com`.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python "niquests>=3.21" "cyclopts>=5.4" "tokenizers>=0.22"
.venv/bin/python -m unittest discover -s tests
ruff check .
pyrefly check
```

`tests/` includes a local HTTP server, so the suite runs offline.

## Agent skill

`skills/tweetcider/` is a drop-in skill for Claude Code, Codex and similar
agents. `SKILL.md` has the intent-to-command picker and how to cite posts in an answer;
`references/` has the CLI reference (flags, exit codes), `mine` setup, endpoints,
recipes and FxEmbed URL modifiers. `scripts/clef-flash-tokenizer.json` (20 MB)
is only read by `mine`.

## License

MIT. See `LICENSE` and `THIRD_PARTY_NOTICES.md`. The command surface is adapted
from `mokouliszt/fxtwitter-skill` (MIT). The FxTwitter API is run by the
FxEmbed project. Not affiliated with X Corp.
