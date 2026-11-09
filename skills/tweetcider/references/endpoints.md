# Endpoints and response shapes

Base: `https://api.fxtwitter.com`. Every payload carries a top-level `code`
mirroring the HTTP status. A `200` with `code: 404` is still a failure.

The live, authoritative schema is `tweetcider get /2/openapi.json`.
This file is the short version.

## Shared envelope

List endpoints return:

```json
{ "code": 200, "results": [ ... ], "cursor": { "bottom": "..." } }
```

`cursor.bottom` is absent or `null` on the last page. Pass it back as `cursor`
(CLI: `--cursor`). Posts in `results` have `type: "status"`, or
`type: "thread"` when `--group-threads` merged a self-reply chain.

An account entry is `{"type": "profile", ...}` with profile fields.

## Posts

| Path | CLI | Args | Notes |
| --- | --- | --- | --- |
| `/2/status/{id}` | `status` | `about_account`, `lang` | Adds `quote`, `translation` when present. |
| `/2/thread/{id}` | `thread` | `about_account`, `lang` | Same shape as status plus `thread[]` (author's self-replies). |
| `/2/conversation/{id}` | `conversation` | `ranking_mode` (`likes`/`recency`), `cursor`, `about_account`, `lang` | `status` + `thread[]` + `replies[]`; paginate replies with `--cursor`. |
| `/2/status/{id}/quotes` | `quotes` | `count`, `cursor`, `lang` | `results[]` of posts. Unreliable on the public instance. |

Raw API shapes (what `--raw` prints; `-f json` tidies them, see `cli-reference.md`).
A post object carries: `type`, `id`, `url`, `text`, `raw_text`, `author`,
`created_at` / `created_timestamp`, `likes`, `reposts` (v1: `retweets`),
`replies`, `quotes`, `views`, `bookmarks`, `lang`, `source`, `media`, and
optionally `quote`, `poll`, `translation`, `card`, `article`, `community`,
`community_note`, `replying_to`, `reposted_by`, `is_note_tweet`,
`possibly_sensitive`.

A deleted, private or withheld post is `{"type": "tombstone", "reason": ...,
"message": ...}` inside `status`, or an entry in `results`.

`media` is an object with any of `photos[]`, `videos[]`, `external`,
`broadcast`, `mosaic`. A video entry has `url`, `width`, `height`, `duration`,
and `formats[]` with `bitrate` + `url`. `--best-variant` picks the highest
bitrate.

## Account timelines

`statuses <handle>` calls `/2/profile/{handle}/statuses`. Handles can be screen
names or `id:<numeric>` user IDs. `--since` returns HTTP 204 (exit 4) when
nothing is newer; values >= 1e12 are converted from milliseconds to seconds.

Other profile endpoints are available through `get`.

## Discovery

| Path | CLI | Args | Notes |
| --- | --- | --- | --- |
| `/2/search` | `search` | `q`, `feed`, `count`, `cursor`, `lang` | `feed` is `latest` or `top`. Unreliable. |
| `/2/typeahead` | `typeahead` | `q`, `result_type`, `src` | Unpaginated. Has `users`, `topics`, `events`, `num_results`. |
| `/2/trends` | `trends` | `type`, `count` | `type` is `trending`; `count` defaults to 20. |

`typeahead` can turn a partial name into a handle.

## Escape hatch

`tweetcider get /2/status/20 --param=lang=ja --param=about_account=1`

`--param` requires `=` so values containing `=` work: `--param=q=a=b` sends
`q=a=b`. Any path the CLI does not wrap is reachable here, including `/i/...`
legacy paths and `/2/openapi.json`.

## Errors

| HTTP / body code | CLI exit | Meaning |
| --- | --- | --- |
| 200 | 0 | OK |
| 204 | 4 | No content for `--since` |
| 400 | 3 | Bad request (bad handle, bad cursor) |
| 401 | 3 | Missing or empty `User-Agent` |
| 404 | 3 | No such post, account or path |
| 429 | 3 | Rate limited; retries honour `Retry-After`, then give up |
| 5xx | 3 | Upstream failure; retried first |

## Compatibility

`/v1/...` paths still work through `get` and return the older `tweet` shape. The
CLI renders `tweet` the same as `status`. FxBluesky uses the same surface on a
different host: `--base-url https://api.fxbluesky.com`.
