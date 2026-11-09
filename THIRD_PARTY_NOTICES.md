# Third-party notices

TweetCider is MIT licensed. It contains work adapted from the projects below.

## mokouliszt/fxtwitter-skill

<https://github.com/mokouliszt/fxtwitter-skill>

The command surface, request shaping, rendering and paging behaviour of
`skills/tweetcider/scripts/tweetcider.py` are adapted from
`skills/fxtwitter/scripts/fxtwitter.py` in that repository. The HTTP layer was
rewritten on niquests and the CLI on cyclopts. No code was copied verbatim from
any other fxtwitter skill repository.

```
MIT License

Copyright (c) 2026 mokouliszt

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Runtime dependencies

Installed by `uv` when the script runs; each keeps its own license.

| Package | License | Use |
| --- | --- | --- |
| `niquests` | Apache-2.0 | Async HTTP client |
| `cyclopts` | Apache-2.0 | CLI parsing and help |
| `tokenizers` | Apache-2.0 | Token counts for `mine` batching |

`skills/tweetcider/scripts/clef-flash-tokenizer.json` is the tokenizer from
`Cloudflare/clef-flash` on Hugging Face (Apache-2.0).

## FxTwitter / FxEmbed

The API is run by the FxEmbed project, <https://github.com/FxEmbed/FxEmbed>
(MIT). TweetCider is not affiliated with it or with X Corp. X and Twitter are
trademarks of X Corp.
