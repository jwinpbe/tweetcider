# FxEmbed URL modifiers

This is the embed side of FxTwitter, not the JSON API. Use it when the user wants
a link that renders properly in Discord, Telegram or Slack. Use the CLI when
they want data.

## Domains

Replace the host of an X permalink; the path stays the same.

| Host | Replaces |
| --- | --- |
| `fxtwitter.com` | `twitter.com` (canonical) |
| `fixupx.com` | `x.com` |
| `fxbsky.app` | `bsky.app` |

```
https://x.com/jack/status/20  ->  https://fixupx.com/jack/status/20
```

## Subdomain prefixes

| Prefix | Effect | Example |
| --- | --- | --- |
| `d.` | Link straight to the media file | `d.fxtwitter.com/jack/status/20` |
| `m.` | Mosaic: several images combined into one | `m.fxtwitter.com/...` |
| `g.` | Gallery: media and author only, no text | `g.fxtwitter.com/...` |
| `t.` | Text only, no media | `t.fxtwitter.com/...` |
| `i.` | Telegram Instant View | `i.fxtwitter.com/...` |
| `o.` | Legacy Discord embed style | `o.fxtwitter.com/...` |

Prefixes apply to the host, not the path. They can be combined with the path
suffixes below.

## Path suffixes

| Suffix | Effect |
| --- | --- |
| `/photo/{n}` | Embed one image from a multi-image post, e.g. `/photo/2` |
| `/{lang}` | Translate the post text, e.g. `/status/20/ja` |
| `.mp4` / `.jpg` | Direct media, equivalent to the `d.` prefix |

## Choosing

| Goal | Use |
| --- | --- |
| Paste a link that should render | a modifier domain |
| Give someone a direct file URL | `d.` prefix, or `.mp4` / `.jpg` suffix |
| Read text, stats, replies, media list | the CLI (`status`, `thread`, `conversation`) |
| Save files to disk | `tweetcider download` |
| Translate for reading | `tweetcider status <id> --lang ja` |
| Translate a link for embedding | `/{lang}` suffix |

## Input normalisation

The CLI accepts any of these forms. A `d.fxtwitter.com/user/status/123/photo/2`
link normalises to status `123`.

```bash
uv run scripts/tweetcider.py status https://d.fxtwitter.com/jack/status/20
uv run scripts/tweetcider.py status https://twitter.com/jack/status/20?s=20
uv run scripts/tweetcider.py status https://x.com/i/status/20
```

## Self-hosted instances

A self-hosted FxEmbed serves the same modifiers on its own host and the same JSON
API at `api.<host>`. Point the CLI at it:

```bash
uv run scripts/tweetcider.py --base-url https://fx.example.com status 20
```

Modifier coverage depends on the instance's configuration, so a prefix that works
on the public instance may be unset on a small deployment.

## Official docs

<https://docs.fxembed.com/guide/url-modifiers/>
