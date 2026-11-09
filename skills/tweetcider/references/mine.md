# mine

`mine <url> "<query>"` fetches available conversation and quote pages, then asks
the `clef-flash` decision model whether each post returned is `useful` for the
query or `chatter`. Conversations may contain only a sample of replies, quote
requests can fail, and paging stops at 1,000 pages. It prints the focal post, always,
then posts with P(useful) at or above `--threshold` (default 0.5), best first,
each with `relevance 0.95` in its header. The last line says how many were
relevant and shown, how many were scored, and the requests and input tokens
billed (`-f json`: the `mined` object).

```bash
uv run scripts/tweetcider.py mine <url> "what evidence do people give" -f md --full-text
uv run scripts/tweetcider.py mine <url> "<query>" --limit 10   # the 10 best only
```

## Reading the result

- Treat it as a ranking, not a verdict. On hand-labelled conversations about 3
  in 4 keep/drop calls matched a human, and fluent AI-written replies score as
  useful: the model cannot spot them. Judge the top posts yourself.
- Thin result: rephrase the query, or lower `--threshold` (0.3) for recall.
  Too many: `--limit N`, which also re-checks the posts near the N-th score.
- Exact repeats (copy-paste bots) are dropped before scoring (`duplicates`).
- `!! quotes unavailable` means the quotes endpoint failed (common on the
  public instance); replies were still scored.
- The fetched posts stay in the response cache (5 minutes), so
  `conversation <url> --all` or `quotes <url> --all` reads them for free.

## Decider setup

Missing credentials exit 2; a decider failure exits 3.

- **Cloudflare Workers AI** (default): `CLOUDFLARE_ACCOUNT_ID` and
  `CLOUDFLARE_API_TOKEN`, or `--cf-key-file FILE` / `TWEETCIDER_CF_KEY_FILE`
  (line 1 account id, line 2 token). Billed per input token: a 400-post
  conversation is about 80k tokens (~270 of the free 10k daily neurons).
- **Local llama.cpp** running the clef-flash GGUF:
  `--decider-url http://host:port/v1/systemone` or `TWEETCIDER_DECIDER_URL`.
  No credentials.

`--decider-budget N` is the tokens per request: default 24000 for Cloudflare
(context 24,576) and 8000 for a custom URL. Keep it under the server's `-ub`
(llama-server answers HTTP 500 "too large to process" otherwise). On a small
GPU, `--decider-budget 4000 --concurrency 1` is the stable setting.

## How scoring works

Posts are batched up to 64 per request, packed to the budget with the bundled
tokenizer (`scripts/clef-flash-tokenizer.json`). A post's score shifts with its
batchmates, so posts within 0.15 of the cutoff are scored again alone
(`rescored_alone`); that costs about 30% more tokens and reduces batch effects
for posts near the cutoff.
