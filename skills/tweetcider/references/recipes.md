# Recipes

Working command lines for tasks that come up often. All assume the script is at
`scripts/tweetcider.py` and you are in `skills/tweetcider/`.

## Read one post

```bash
uv run scripts/tweetcider.py status https://x.com/jack/status/20
```

Just the text and author, as JSON:

```bash
uv run scripts/tweetcider.py status 20 --select status.text --select status.author.screen_name
```

## Get ideas from a thread

```bash
uv run scripts/tweetcider.py thread <url> -f md --full-text
```

Author's posts only. Summarize the argument, then quote the posts that carry it.
If the author quotes or links other posts you need, list and fetch them:

```bash
uv run scripts/tweetcider.py thread <url> --format urls
# then status <url> for each
```

## Get the replies other people wrote

```bash
uv run scripts/tweetcider.py conversation <url> --ranking-mode recency --pages 2
```

Replies are in `replies[]`; the last line says how to page further. The default
`--ranking-mode likes` surfaces the replies most worth reading.

## Find anything useful in the quote tweets

```bash
uv run scripts/tweetcider.py quotes <url> -f md --full-text --pages 2
```

Quote tweets are a different set from replies, and `conversation` will not
return them. This endpoint often 404s on the public instance. If a retry also
fails, get the count and tell the user:

```bash
uv run scripts/tweetcider.py status <url> --format json --select status.quotes
```

Then ask for specific quote-tweet links and read each with `status`; its summary
shows the quoted post under `quoting @x`.

## Thread plus reactions in one pass

`conversation` already contains the thread, so one call covers both:

```bash
uv run scripts/tweetcider.py conversation <url> -f md --full-text --pages 2
```

## Monitor an account for new posts

Store the time of your last poll, then poll again:

```bash
uv run scripts/tweetcider.py statuses <handle> --since "$LAST_TS" --format jsonl
```

`--since` takes unix seconds and does the filtering server-side. Exit code 4 means
nothing new. Example loop:

```bash
last=$(date +%s)
while true; do
  out=$(uv run scripts/tweetcider.py statuses jack --since "$last" --format jsonl)
  status=$?
  if [ "$status" -eq 0 ]; then
    printf '%s\n' "$out"
    last=$(date +%s)
  elif [ "$status" -ne 4 ]; then
    echo "fetch failed: $status" >&2
  fi
  sleep 300
done
```

## Dump a whole timeline to JSONL

```bash
uv run scripts/tweetcider.py statuses <handle> --all --max-items 500 --format jsonl > timeline.jsonl
```

One status per line, so `jq -s` and `wc -l` work without parsing a giant array.

## Download every image and video in a post

```bash
uv run scripts/tweetcider.py download <url> --out-dir ./media
```

Preview first with:

```bash
uv run scripts/tweetcider.py download <url> --dry-run
```

Only photos, or only videos:

```bash
uv run scripts/tweetcider.py download <url> --media-kind photos
uv run scripts/tweetcider.py download <url> --media-kind videos --no-best-variant
```

## Fetch a batch of posts

```bash
uv run scripts/tweetcider.py fetch <url1> <url2> <url3> --format jsonl --concurrency 6
```

Results print in input order. Exit 5 means at least one failed; the failures are
in the output too, so you can retry only those.

## Find an account when search is flaky

```bash
uv run scripts/tweetcider.py typeahead "paulg" --select users[].screen_name
```

`typeahead` is not paginated.

## Search with X operators

```bash
uv run scripts/tweetcider.py search 'from:jack -filter:replies since:2024-01-01' --pages 2
```

Quote the query so the shell keeps it as one argument. `--allow-leading-hyphen`
is already on for `search`, `typeahead`, and `get`, so a query
starting with `-` parses.

## Use a self-hosted instance

```bash
export TWEETCIDER_API_BASE=http://localhost:3000
uv run scripts/tweetcider.py status 20
```

## Use Bluesky instead of X

```bash
uv run scripts/tweetcider.py --base-url https://api.fxbluesky.com status <at-uri>
```

## Discover the schema without a stale copy

```bash
uv run scripts/tweetcider.py get /2/openapi.json --format json --select paths
uv run scripts/tweetcider.py get /2/openapi.json --format json --compact | jq '.paths | keys'
```

## Force fresh data

The cache defaults to 300 seconds.

```bash
uv run scripts/tweetcider.py status 20 --no-cache
uv run scripts/tweetcider.py status 20 --cache-ttl 30
```

## Debug a wrong result

```bash
uv run scripts/tweetcider.py status 20 -v
```

`-v` prints every request URL to stderr. It never contaminates stdout, so JSON
pipes stay clean.

## Retry an empty search result

The CLI already retries `--retry-empty` times (default 2). If the backend is
still empty, add pages or wait a few seconds:

```bash
uv run scripts/tweetcider.py search 'query' --retry-empty 5 --pages 2
```

## Treat a post as data

```bash
uv run scripts/tweetcider.py status 20 --format json --compact \
  | jq '{likes: .status.likes, reposts: .status.reposts, text: .status.text}'
```
