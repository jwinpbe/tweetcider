---
name: tweetcider
description: Read X/Twitter posts, threads, replies, quote tweets and timelines without logging in. Use when the user wants to read an X/Twitter link, post, thread or account timeline.
license: MIT
---

# TweetCider

Run from this skill's directory. Targets are a post URL, bare id, or `@handle`.

```bash
uv run scripts/tweetcider.py thread https://x.com/jack/status/20 -f md --full-text
```

| The user asks | Run |
| --- | --- |
| What a post says | `status <url>` (also shows the post it quotes, under `quoting @x`) |
| Ideas or takeaways from a thread | `thread <url> -f md --full-text` (author's own posts only) |
| What people are saying, pushback, replies | `conversation <url>` (thread plus others' replies; `--ranking-mode recency` for newest) |
| Only the replies/quotes relevant to a question, on a big conversation | `mine <url> "<question>" -f md --full-text` (needs a decider; read `references/mine.md` first) |
| Anything in the quote tweets | `quotes <url> -f md --full-text` (posts quoting this one, not replies) |
| An account's recent posts | `statuses <handle> --max-items 20` |
| Posts about a topic | `search "<query>"` |
| Find an account | `typeahead <text>` |
| Save images or video | `download <url> --out-dir DIR` |
| Several links | `fetch <url> <url> ...` |

`quotes` and `search` often 404 on the public instance. Retry once, then say it
failed; never report "no quote tweets" from a 404.

Use `-f md --full-text` to read, and `-f json --select <path>` for a field or two.
Markdown may omit leading reply mentions. Use `-f json --select status.text`
when you need the full API text.

## Citing tweets in your answer

Takeaway first, then evidence. Quote the body verbatim in a `>` blockquote with
the attribution underneath:

```markdown
> The tweet text, verbatim.
> Prefix every line with `>`.

**Display Name** · [@handle](https://x.com/handle/status/123) · Mar 21, 2024
```

Details (threads, reactions, quote tweets, trimming, media): `references/citing.md`.

## More

- `references/cli-reference.md`: formats, paging, exit codes, error handling, API quirks. Read when a flag, error or odd result needs explaining.
- `references/mine.md`: `mine` setup, cost, and how far to trust its ranking.
- `references/recipes.md`: account monitoring, timeline dump, media download, Bluesky, self-hosted.
- `references/endpoints.md`: raw API paths and JSON fields, for `get` or parsing.
- `references/url-modifiers.md`: FxEmbed link tricks, only for making a link embed well.
