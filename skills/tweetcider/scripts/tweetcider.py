#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["niquests>=3.21", "cyclopts>=5.4", "tokenizers>=0.22"]
# ///
"""TweetCider - read X/Twitter posts, threads and timelines through FxTwitter.

    uv run tweetcider.py status https://x.com/jack/status/20
    uv run tweetcider.py thread <url> --format md --full-text
    uv run tweetcider.py fetch <url> <url> <url> --format jsonl

No API key and no login. Public data only. MIT licensed; the FxTwitter v2
surface is adapted from mokouliszt/fxtwitter-skill (MIT).
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import os
import pathlib
import re
import sqlite3
import sys
import time
import urllib.parse
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache, partial
from itertools import chain, pairwise
from typing import Annotated, Any, Literal

import niquests
from cyclopts import App, Parameter, validators
from tokenizers import Tokenizer

__version__ = "1.0.0"

DEFAULT_BASE = "https://api.fxtwitter.com"
DEFAULT_UA = f"tweetcider/{__version__}"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MEDIA_DIR = pathlib.Path("./tweetcider-media")
CLEF_URL = "https://api.cloudflare.com/client/v4/accounts/{}/ai/run/@cf/cloudflare/clef-flash"

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_API_ERROR = 3
EXIT_NO_CONTENT = 4
EXIT_PARTIAL = 5


class TweetciderError(Exception):
    """A failure that ends the process with a message, not a traceback."""

    def __init__(self, message: str, code: int = EXIT_RUNTIME):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Input normalisation
# --------------------------------------------------------------------------- #

_STATUS_MARKERS = ("status", "statuses", "post")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def _is_snowflake(value: str) -> bool:
    """A bare post id: ASCII digits, at most 25 of them."""
    return value.isascii() and value.isdigit() and len(value) <= 25


def parse_status_id(value: str) -> str:
    """Bare snowflake id or any X/FxEmbed permalink -> id."""
    value = (value or "").strip()
    if not value:
        raise TweetciderError("empty status id", EXIT_USAGE)
    if _is_snowflake(value):
        return value
    candidate = value if "//" in value else "https://" + value
    path = urllib.parse.urlsplit(candidate).path
    segments = path.split("/")
    for segment, following in pairwise(segments):
        if segment in _STATUS_MARKERS and _is_snowflake(following):
            return following
    # Media permalinks end in `/20.jpg`, `/20.mp4`, etc.
    stem = pathlib.PurePosixPath(path).stem
    if _is_snowflake(stem):
        return stem
    raise TweetciderError(f"no status id found in {value!r}", EXIT_USAGE)


def parse_handle(value: str) -> str:
    """@name, name, profile URL, or id:<numeric id> -> handle (or id:<n>)."""
    value = (value or "").strip()
    if not value:
        raise TweetciderError("empty handle", EXIT_USAGE)
    scheme, sep, rest = value.partition(":")
    if sep and scheme.lower() == "id":
        rest = rest.strip()
        if not rest.isdigit():
            raise TweetciderError(f"`id:` needs a numeric user id, got {value!r}", EXIT_USAGE)
        return "id:" + rest
    if value.isdigit():
        return "id:" + value
    value = value.removeprefix("@")
    if "/" in value or "." in value:
        parsed = urllib.parse.urlsplit(value if "//" in value else "https://" + value)
        last = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        value = (last or parsed.netloc).removeprefix("@")
    if not _HANDLE_RE.match(value):
        raise TweetciderError(
            f"{value!r} is not a valid X handle (1-15 chars, letters/digits/underscore); "
            "use `id:<numeric id>` for numeric lookups.",
            EXIT_USAGE,
        )
    return value


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

class Cache:
    """SQLite response cache. Any failure disables it; a cache never breaks a fetch."""

    def __init__(self, ttl: float):
        self.ttl = max(0.0, ttl)
        self.db: sqlite3.Connection | None = None
        if not self.ttl:
            return
        cache_root = pathlib.Path(os.environ.get("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache")
        directory = pathlib.Path(os.environ.get("TWEETCIDER_CACHE_DIR") or cache_root / "tweetcider")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(directory / "cache.db", check_same_thread=False)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS replies (url TEXT PRIMARY KEY, status INTEGER, body TEXT, fetched REAL)"
            )
            # Every distinct URL adds a row; drop what no run would read again.
            self.db.execute("DELETE FROM replies WHERE fetched < ?", (time.time() - max(self.ttl, 86400),))
            self.db.commit()
        except (sqlite3.Error, OSError):
            self.db = None

    def get(self, url: str) -> tuple[int, Any] | None:
        if self.db is None:
            return None
        try:
            row = self.db.execute("SELECT status, body, fetched FROM replies WHERE url=?", (url,)).fetchone()
        except sqlite3.Error:
            return None
        if not row or time.time() - row[2] > self.ttl:
            return None
        try:
            return int(row[0]), json.loads(row[1])
        except (ValueError, TypeError):
            return None

    def put(self, url: str, status: int, payload: Any) -> None:
        if self.db is None:
            return
        try:
            self.db.execute(
                "INSERT OR REPLACE INTO replies VALUES (?, ?, ?, ?)",
                (url, status, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            self.db.commit()
        except (sqlite3.Error, TypeError):
            pass

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #

@dataclass
class Reply:
    status: int
    payload: Any
    url: str
    cached: bool = False


class Client:
    def __init__(self, opts: Opts):
        self.base = opts.base_url.rstrip("/")
        self.timeout = opts.timeout
        self.verbose = opts.verbose
        self.cache = Cache(opts.cache_ttl if opts.cache else 0)
        retries = niquests.RetryConfiguration(
            total=max(0, opts.retries),
            status_forcelist=(429, 500, 502, 503, 504),
            respect_retry_after_header=True,
            backoff_factor=0.75,
            backoff_jitter=0.5,
            raise_on_status=False,
            allowed_methods=None,  # also retry the decider's POST; scoring is idempotent
        )
        self.session = niquests.AsyncSession(
            headers={"User-Agent": opts.user_agent or DEFAULT_UA, "Accept": "application/json"},
            retries=retries,
        )

    def build_url(self, path: str, params: dict | None = None) -> str:
        url = path if path.startswith(("http://", "https://")) else self.base + "/" + path.lstrip("/")
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        if clean:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)
        return url

    async def get(self, path: str, params: dict | None = None) -> Reply:
        url = self.build_url(path, params)
        hit = self.cache.get(url)
        if hit is not None and not looks_empty(hit[1]):
            return Reply(hit[0], hit[1], url, cached=True)
        if self.verbose:
            print(f"[tweetcider] GET {url}", file=sys.stderr)
        try:
            response = await self.session.get(url, timeout=self.timeout)
            raw = await read_body(response)
        except (niquests.exceptions.RequestException, OSError) as exc:
            raise TweetciderError(f"network error requesting {url}: {exc}")

        status = response.status_code or 0
        if status == 401 and not raw:
            raise TweetciderError(
                "401 Unauthorized - the API requires a User-Agent header. Set --user-agent.",
                EXIT_API_ERROR,
            )
        payload = decode(raw or b"", status, url)
        if status == 200 and isinstance(payload, dict) and payload.get("code") == 200 and not looks_empty(payload):
            self.cache.put(url, status, payload)
        return Reply(status, payload, url)

    async def aclose(self) -> None:
        self.cache.close()
        await self.session.close()


async def read_body(response: Any) -> bytes | None:
    """niquests hands back bytes, or an awaitable when the body is still streaming."""
    raw = response.content
    return await raw if inspect.isawaitable(raw) else raw


def decode(raw: bytes, status: int, url: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        snippet = raw[:200].decode("utf-8", errors="replace")
        code = EXIT_API_ERROR if not 200 <= status < 300 else EXIT_RUNTIME
        raise TweetciderError(f"non-JSON response (HTTP {status}) from {url}: {exc}; body starts {snippet!r}", code)


def _count(value: Any) -> str:
    """2084 -> 2.1k, 87171 -> 87k: counts people read at a glance."""
    if not isinstance(value, (int, float)):
        return "-" if value is None else str(value)
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= div:
            short = value / div
            return (f"{short:.0f}" if short >= 10 else f"{short:.1f}".removesuffix(".0")) + unit
    return f"{value:,}"


def _iso(value: Any) -> Any:
    """X's `Fri Nov 06 22:50:41 +0000 2026` -> `2026-11-06T22:50:41Z`; anything else unchanged."""
    if not isinstance(value, str):
        return value
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return value


def _date(status: dict[str, Any], fmt: str = "%Y-%m-%d %H:%M UTC") -> str:
    ts = status.get("created_timestamp")
    if isinstance(ts, (int, float)):
        return time.strftime(fmt, time.gmtime(ts))
    return str(_iso(status.get("created_at")) or "-")


def _clip(text: str, limit: int | None) -> str:
    text = (text or "").strip()
    if limit and len(text) > limit + 100:  # cutting less than a sentence or two saves nothing
        return text[:limit].rstrip() + f"... [+{len(text) - limit} chars, --full-text]"
    return text


def _body(status: dict[str, Any]) -> str:
    """The post's own words: the leading @mentions X adds to a reply are dropped (the API marks where they end)."""
    text = status.get("text") or ""
    start = ((status.get("raw_text") or {}).get("display_text_range") or [0])[0]
    return text[start:] if start and re.fullmatch(r"(@\w+\s*)+", text[:start]) else text


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _stats(status: dict[str, Any]) -> str:
    parts = []
    for key, one in (("likes", "like"), ("replies", "reply"), ("reposts", "repost"), ("quotes", "quote"),
                     ("views", "view")):
        value = status.get(key)
        if isinstance(value, int) and value > 0:
            parts.append(f"{_count(value)} {one if value == 1 else key}")
    return " · ".join(parts)


def _who(author: dict[str, Any]) -> str:
    handle = author.get("screen_name") or "?"
    name = author.get("name") or ""
    return f"@{handle}" + (f" ({name})" if name and name.lower() != handle.lower() else "")


def _media_summary(status: dict[str, Any]) -> list[str]:
    media = status.get("media") or {}
    lines: list[str] = []
    for photo in media.get("photos") or []:
        alt = photo.get("altText")
        lines.append(f"photo {photo.get('width')}x{photo.get('height')} {photo.get('url')}" + (f" alt={alt!r}" if alt else ""))
    for video in media.get("videos") or []:
        lines.append(f"{video.get('type', 'video')} {video.get('duration')}s {video.get('url')}")
    external = media.get("external")
    if external:
        lines.append(f"external {external.get('type')} {external.get('url')}")
    broadcast = media.get("broadcast")
    if broadcast:
        lines.append(f"broadcast [{broadcast.get('state')}] {broadcast.get('title')} {broadcast.get('url')}")
    mosaic = media.get("mosaic")
    if mosaic and not lines:
        lines.append(f"mosaic {mosaic.get('url')}")
    return lines


def format_status(
    status: dict[str, Any], text_limit: int | None, indent: str = "", *, seen: set | None = None, lead: str = ""
) -> str:
    """One post as a block: header line, `|` text, extras, URL last. `seen` holds ids already printed, so a
    quoted or parent post printed earlier is referred to instead of repeated."""
    if status.get("type") == "tombstone":
        return f"{indent}{lead}(unavailable: {status.get('reason')}) {status.get('url') or status.get('id') or ''}".rstrip()
    if status.get("type") == "thread":  # grouped timeline entry from `--group-threads`
        posts = status.get("statuses") or []
        head = f"{indent}{lead}thread {status.get('conversation_id')} · {_n(len(posts), 'post')}" + (
            " (truncated)" if status.get("truncated") else ""
        )
        return "\n".join([head, *(format_status(s, text_limit, indent + "  ", seen=seen) for s in posts)])

    sub = indent + "  "
    header = f"{indent}{lead}{_who(status.get('author') or {})} · {_date(status)}"
    stats = _stats(status)
    if stats:
        header += " · " + stats
    if "relevance" in status:
        header += f" · relevance {status['relevance']:.2f}"
    parts = [header]
    reposted = status.get("reposted_by")
    if reposted:
        parts.append(f"{sub}reposted by @{reposted.get('screen_name')}")
    replying = status.get("replying_to")
    if replying and (seen is None or replying.get("status") not in seen):
        parts.append(f"{sub}replying to @{replying.get('screen_name')}")
    text = _clip(_body(status), text_limit)
    if text:
        parts.append("\n".join(f"{sub}| {line}" for line in text.splitlines()))
    translation = status.get("translation")
    if translation:
        translated = _clip(translation.get("text") or "", text_limit)
        parts.extend((f"{sub}translated from {translation.get('source_lang')}:",
                      "\n".join(f"{sub}| {line}" for line in translated.splitlines())))
    poll = status.get("poll")
    if poll:
        choices = " · ".join(f"{c.get('label')} {c.get('percentage')}%" for c in poll.get("choices") or [])
        parts.append(f"{sub}poll ({_count(poll.get('total_votes'))} votes, {poll.get('time_left_en') or 'ended'}): {choices}")
    note = status.get("community_note")
    if note:
        parts.append(f"{sub}community note: {_clip(note.get('text') or '', text_limit)}")
    if status.get("community"):
        parts.append(f"{sub}community: {status['community'].get('name')}")
    if status.get("article"):
        parts.append(f"{sub}article: {status['article'].get('title')}")
    card = status.get("card") or status.get("twitter_card")
    if isinstance(card, dict):
        parts.append(f"{sub}link: {card.get('title')} <{card.get('url')}>")
    parts.extend(sub + line for line in _media_summary(status))
    if status.get("possibly_sensitive"):
        parts.append(f"{sub}(possibly sensitive)")
    quote = status.get("quote")
    if isinstance(quote, dict):
        if seen is not None and quote.get("id") in seen:
            parts.append(f"{sub}quoting {_who(quote['author']) if quote.get('author') else quote.get('url')}, shown above")
        else:
            parts.append(format_status(quote, text_limit, sub, seen=seen, lead="quoting "))
    if status.get("url"):
        parts.append(f"{sub}{status['url']}")
    if seen is not None and status.get("id"):
        seen.add(status["id"])
    return "\n".join(parts)


def format_user(user: dict[str, Any], text_limit: int | None) -> str:
    head = [_who(user)]
    verification = user.get("verification") or {}
    if verification.get("verified"):
        head.append(f"verified {verification.get('type') or ''}".rstrip())
    if user.get("protected"):
        head.append("protected")
    if user.get("joined"):
        head.append("joined " + str(_iso(user["joined"]))[:10])
    if user.get("location"):
        head.append(user["location"])
    counts = " · ".join(
        f"{_count(user.get(key))} {label}"
        for key, label in (("followers", "followers"), ("following", "following"), ("statuses", "posts"),
                           ("media_count", "media"), ("likes", "likes"))
        if user.get(key) is not None
    )
    lines = [" · ".join(head)]
    if counts:
        lines.append("  " + counts)
    description = _clip(user.get("description") or "", text_limit)
    lines.extend(f"  | {line}" for line in description.splitlines())
    website = (user.get("website") or {}).get("url")
    if website:
        lines.append(f"  website {website}")
    birthday = user.get("birthday")
    if birthday:
        lines.append(f"  birthday {birthday.get('year') or '?'}-{birthday.get('month')}-{birthday.get('day')}")
    if user.get("about_account"):
        lines.append("  " + format_about(user["about_account"]))
    if user.get("url"):
        lines.append(f"  {user['url']}")
    return "\n".join(lines)


def format_about(about: dict[str, Any]) -> str:
    changes = about.get("username_changes") or {}
    parts = [f"based in {about.get('based_in') or '?'}"]
    if about.get("source"):
        parts.append(f"via {about['source']}")
    if changes.get("count"):
        parts.append(f"{changes['count']} username changes (last {changes.get('last_changed_at')})")
    return "about: " + " · ".join(parts)


def merge_status(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """Fill missing focal-post fields from its thread copy; focal values win conflicts."""
    merged = fallback.copy()
    for key, value in primary.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_status(value, merged[key])
        elif value is not None or key not in merged:
            merged[key] = value
    return merged


def focal_and_thread(payload: dict[str, Any]) -> tuple[Any, list, bool]:
    focal = payload.get("status")
    thread = payload.get("thread") or []
    focal_id = focal.get("id") if isinstance(focal, dict) else None
    matched = focal_id is not None and any(
        isinstance(post, dict) and post.get("id") == focal_id for post in thread
    )
    if matched and isinstance(focal, dict):
        thread = [merge_status(focal, post) if isinstance(post, dict) and post.get("id") == focal_id else post
                  for post in thread]
    return focal, thread, matched


def _reply_tree(replies: list[dict]) -> list[tuple[int, dict]]:
    """(depth, post) in reading order: each reply right under the reply it answers, when that one is in the list."""
    ids = {r.get("id") for r in replies}
    kids: dict[Any, list[dict]] = {}
    for reply in replies:
        parent = (reply.get("replying_to") or {}).get("status")
        kids.setdefault(parent if parent in ids and parent != reply.get("id") else None, []).append(reply)
    order: list[tuple[int, dict]] = []
    done: set = set()

    def walk(post: dict, depth: int) -> None:
        if post.get("id") in done:
            return
        done.add(post.get("id"))
        order.append((depth, post))
        for kid in kids.get(post.get("id"), []):
            walk(kid, depth + 1)

    for post in chain(kids.get(None, []), replies):  # the second pass picks up reply cycles, which have no root
        walk(post, 0)
    return order


def _more_hint(payload: dict[str, Any]) -> str | None:
    cursor = payload.get("cursor")
    if not isinstance(cursor, dict) or not cursor.get("bottom") or "trends" in payload:
        return None
    pages = payload.get("__pages_fetched__") or 1
    return f"more: --pages {pages + 1} or --all (resume cursor: -f json --select cursor.bottom)"


def _mined_line(mined: dict[str, Any]) -> str:
    line = (
        f"mined {json.dumps(mined.get('query'), ensure_ascii=False)} · {mined.get('shown')} of {mined.get('relevant')} "
        f"relevant shown · {mined.get('scored')} scored"
    )
    if mined.get("duplicates"):
        line += f", {mined['duplicates']} duplicates dropped"
    return line + (
        f" · threshold {mined.get('threshold')} · {mined.get('requests')} requests, "
        f"{_count(mined.get('input_tokens'))} input tokens"
    )


def format_summary(payload: dict[str, Any], text_limit: int | None) -> str:
    """Render any v2 or v1 payload as compact plain text."""
    if payload.get("__no_content__"):
        return "204 No Content - nothing newer than the given `since` timestamp."

    seen: set = set()
    fmt = partial(format_status, text_limit=text_limit, seen=seen)
    out: list[str] = []
    code = payload.get("code")
    if code is not None and code != 200:
        out.append(f"!! HTTP {code}: {payload.get('message') or 'error'}")
        if payload.get("reason"):
            out.append(f"!! reason: {payload['reason']}")

    status, thread, focal_in_thread = focal_and_thread(payload)
    focal_id = status.get("id") if isinstance(status, dict) else None
    if isinstance(status, dict) and not focal_in_thread:
        out.append(fmt(status))
    if len(thread) == 1 and focal_in_thread:
        out.append(fmt(thread[0]))
    elif thread:
        out.append(f"== thread · {_n(len(thread), 'post')}")
        out.extend(fmt(post, lead="▶ " if focal_in_thread and post.get("id") == focal_id else "")
                   for post in thread if isinstance(post, dict))
    if payload.get("replies") is not None:
        replies = [r for r in payload["replies"] if isinstance(r, dict)]
        total = status.get("replies") if isinstance(status, dict) else None
        shown = f"{len(replies)} of {total}" if isinstance(total, int) and total > len(replies) else str(len(replies))
        out.append(f"== replies · {shown}")
        out.extend(fmt(post, indent="  " * min(depth, 6), lead="↳ " if depth else "")
                   for depth, post in _reply_tree(replies))

    if payload.get("tweet") is not None:  # v1 shape
        out.append(fmt(payload["tweet"]))

    if isinstance(payload.get("results"), list):
        results = [r for r in payload["results"] if isinstance(r, dict)]
        kinds = {r.get("type") for r in results}
        label = "user" if kinds == {"profile"} else "relevant post" if payload.get("mined") else "post"
        out.append(f"== {_n(len(results), label)}")
        out.extend(format_user(r, text_limit) if r.get("type") == "profile" else fmt(r) for r in results)

    if payload.get("user"):
        out.append(format_user(payload["user"], text_limit))
    if payload.get("about_account") and "user" not in payload:
        out.append(format_about(payload["about_account"]))

    if "num_results" in payload:  # typeahead
        out.append(f"== typeahead {payload.get('query')!r} · {payload.get('num_results')} results")
        for user in payload.get("users") or []:
            verified = " · verified" if (user.get("verification") or {}).get("verified") else ""
            out.append(f"  {_who(user)}{verified}")
        out.extend(f"  topic {t.get('topic')}" for t in payload.get("topics") or [])
        out.extend(f"  event {e.get('topic')} - {e.get('supporting_text')} {e.get('url') or ''}".rstrip()
                   for e in payload.get("events") or [])

    if payload.get("trends") is not None:
        out.append(f"== {_n(len(payload['trends']), 'trend')}")
        for i, trend in enumerate(payload["trends"], 1):
            grouped = ", ".join(g.get("name", "") for g in (trend.get("grouped_topics") or []))
            out.append(f"  {i:>2}. {trend.get('name')} ({trend.get('context')})" + (f" [{grouped}]" if grouped else ""))

    if payload.get("mined"):
        out.append("== " + _mined_line(payload["mined"]))
        if payload["mined"].get("warning"):
            out.append(f"!! {payload['mined']['warning']}")
    hint = _more_hint(payload)
    if hint:
        out.append("== " + hint)
    if not out:
        out.append(json.dumps(payload, ensure_ascii=False, indent=2))
    return "\n".join(out)


def format_markdown(payload: dict[str, Any], text_limit: int | None) -> str:
    """Markdown in the citing.md shape: verbatim `>` body, attribution line under it. Paste-ready."""
    lines: list[str] = []
    seen: set = set()

    def attribution(status: dict[str, Any]) -> str:
        author = status.get("author") or {}
        handle = author.get("screen_name") or "?"
        link = f"[@{handle}]({status['url']})" if status.get("url") else f"@{handle}"
        parts = [f"**{author.get('name') or handle}**", link, _date(status, "%b %d, %Y")]
        replying = status.get("replying_to")
        if replying and replying.get("status") != root:  # md is flat, so name every parent but the post itself
            parts.append(f"replying to @{replying.get('screen_name')}")
        parts.extend(p for p in (_stats(status),) if p)
        if "relevance" in status:
            parts.append(f"relevance {status['relevance']:.2f}")
        return " · ".join(parts)

    def quoted(status: dict[str, Any], depth: int = 1) -> list[str]:
        """The `>`-prefixed body of a post, its media and the post it quotes, one level deeper."""
        mark = ">" * depth
        if status.get("type") == "tombstone":
            return [f"{mark} *(unavailable: {status.get('reason')})*"]
        body = [f"{mark} {line}".rstrip() for line in _clip(_body(status), text_limit).splitlines()]
        media = status.get("media") or {}
        body += [f"{mark} *(photo)* {p.get('url')}" for p in media.get("photos") or []]
        body += [f"{mark} *({v.get('type') or 'video'}, {v.get('duration')}s)* {v.get('url')}" for v in media.get("videos") or []]
        card = status.get("card") or status.get("twitter_card")
        if isinstance(card, dict) and card.get("url"):
            body.append(f"{mark} *(link)* [{card.get('title') or card['url']}]({card['url']})")
        quote = status.get("quote")
        if isinstance(quote, dict):
            if quote.get("id") in seen:
                who = _who(quote["author"]) if quote.get("author") else quote.get("url")
                body += [mark, f"{mark} *(quoting {who}, above)*"]
            else:
                body += [mark, *quoted(quote, depth + 1), mark + ">", f"{mark}> {attribution(quote)}"]
                if quote.get("id"):
                    seen.add(quote["id"])
        return body

    def post(status: dict[str, Any]) -> None:
        if status.get("type") == "thread":
            for sub in status.get("statuses") or []:
                post(sub)
            return
        lines.extend((*quoted(status), "", attribution(status) if status.get("type") != "tombstone" else "", ""))
        if status.get("id"):
            seen.add(status["id"])

    focal, thread, matched = focal_and_thread(payload)
    root = focal.get("id") if isinstance(focal, dict) else None
    if isinstance(focal, dict) and not matched:
        post(focal)
    if payload.get("tweet"):
        post(payload["tweet"])
    if thread:
        if len(thread) > 1:
            lines.extend((f"## Thread · {_n(len(thread), 'post')}", ""))
        for status in thread:
            post(status)
    if payload.get("replies"):
        total = focal.get("replies") if isinstance(focal, dict) else None
        count = len(payload["replies"])
        lines.extend((f"## Replies · {count}" + (f" of {total}" if isinstance(total, int) and total > count else ""), ""))
        for _, status in _reply_tree([r for r in payload["replies"] if isinstance(r, dict)]):
            post(status)
    results = [r for r in payload.get("results") or [] if isinstance(r, dict)]
    if results:
        lines.extend((f"## {'Relevant posts' if payload.get('mined') else 'Results'} · {len(results)}", ""))
    for item in results:
        if item.get("type") == "profile":
            lines.extend((f"- [{_who(item)}]({item.get('url')}) · {_count(item.get('followers'))} followers", ""))
        else:
            post(item)
    user = payload.get("user")
    if user:
        counts = (f"{_count(user.get('followers'))} followers · {_count(user.get('following'))} following · "
                  f"{_count(user.get('statuses'))} posts")
        lines.extend((f"## [{_who(user)}]({user.get('url')})", "", _clip(user.get("description") or "", text_limit), "",
                      counts, ""))
    for i, trend in enumerate(payload.get("trends") or [], 1):
        lines.append(f"{i}. **{trend.get('name')}** - {trend.get('context')}")
    for found in payload.get("users") or []:  # typeahead
        lines.append(f"- [{_who(found)}](https://x.com/{found.get('screen_name')})")
    if payload.get("mined"):
        lines.extend(("", f"*{_mined_line(payload['mined'])}*"))
    hint = _more_hint(payload)
    if hint:
        lines.extend(("", f"*{hint}*"))
    if not [line for line in lines if line]:
        lines = ["```json", json.dumps(payload, ensure_ascii=False, indent=2), "```"]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


_UNSHOWN = {"raw_text", "raw_description", "embed_card", "provider", "is_note_tweet", "banner_url", "source"}
_AUTHOR_FIELDS = ("id", "screen_name", "name", "followers", "following", "statuses", "joined")


def tidy(payload: Any) -> Any:
    """The JSON for `-f json`/`jsonl`/`--select`: no nulls, empty objects or formatting-only fields, ISO dates,
    a compact `author` per post, and a post quoted again later reduced to its id and url. `--raw` skips this."""
    seen: set = set()

    def clean(value: Any, key: str = "") -> Any:
        if isinstance(value, list):
            return [clean(v) for v in value]
        if not isinstance(value, dict):
            return _iso(value) if key in ("created_at", "joined") else value
        if key == "author":
            slim = {k: value[k] for k in _AUTHOR_FIELDS if value.get(k) is not None}
            if (value.get("verification") or {}).get("verified"):
                slim["verified"] = True
            return clean(slim)
        if key == "replying_to":
            return {k: value[k] for k in ("screen_name", "status") if k in value}
        if key == "quote" and value.get("id") in seen:
            return {"id": value["id"], "url": value.get("url"), "shown_above": True}
        if value.get("type") in ("status", "substatus") and value.get("id"):
            seen.add(value["id"])
        out = {}
        for k, v in value.items():
            if k in _UNSHOWN or k.startswith("__") or v is None or v == {} or v == "":
                continue
            out[k] = clean(v, k)
        return out

    if isinstance(payload, dict) and isinstance(payload.get("status"), dict):
        payload = {k: v for k, v in payload.items() if k != "author"}  # the same account as status.author
    return clean(payload)


def collect_statuses(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten every status-like object in a payload, including grouped threads."""
    found: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if not isinstance(item, dict):
            return
        if item.get("type") == "thread":
            for sub in item.get("statuses") or []:
                visit(sub)
            return
        if item.get("type") in ("status", "substatus"):
            found.append(item)
            return
        # Legacy v1 payloads carry no `type` discriminator.
        if "id" in item and "text" in item and item.get("type") != "tombstone":
            found.append(item)

    focal, thread, matched = focal_and_thread(payload)
    if not matched:
        visit(focal)
    visit(payload.get("tweet"))
    for item in thread:
        visit(item)
    for key in ("replies", "results"):
        for item in payload.get(key) or []:
            visit(item)
    return found


def format_urls(payload: dict[str, Any]) -> str:
    urls = []
    for status in collect_statuses(payload):
        if status.get("url"):
            urls.append(status["url"])
    for item in payload.get("results") or []:
        if isinstance(item, dict) and item.get("type") == "profile" and item.get("url"):
            urls.append(item["url"])
    if payload.get("user", {}).get("url"):
        urls.append(payload["user"]["url"])
    return "\n".join(dict.fromkeys(urls))


def media_urls(payload: dict[str, Any], kind: str = "all", best: bool = True) -> list[tuple[str, str]]:
    """Return (url, suggested_filename) pairs for every media item in a payload."""
    out: list[tuple[str, str]] = []
    for status in collect_statuses(payload):
        media = status.get("media") or {}
        sid = status.get("id") if _is_snowflake(str(status.get("id"))) else "unknown"  # it becomes a file name
        if kind in ("all", "photos"):
            for index, photo in enumerate(media.get("photos") or [], 1):
                url = photo.get("url")
                if not url:
                    continue
                ext = _extension_from_url(url, "jpg")
                out.append((url, f"{sid}_photo{index}.{ext}"))
        if kind in ("all", "videos"):
            for index, video in enumerate(media.get("videos") or [], 1):
                url = video.get("url")
                formats = video.get("formats") or []
                if best and formats:
                    url = max(formats, key=lambda f: f.get("bitrate") or 0).get("url") or url
                if not url:
                    continue
                ext = _extension_from_url(url, "mp4")
                out.append((url, f"{sid}_video{index}.{ext}"))
        if kind == "all" and media.get("external", {}).get("url"):
            out.append((media["external"]["url"], f"{sid}_external"))
    return out


def _extension_from_url(url: str, default: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    suffix = pathlib.PurePosixPath(parsed.path).suffix.removeprefix(".")
    if suffix.isascii() and suffix.isalnum() and 2 <= len(suffix) <= 5:
        return suffix.lower()
    fmt = urllib.parse.parse_qs(parsed.query).get("format", [default])[0]
    return fmt.lower() if fmt.isascii() and fmt.isalnum() and len(fmt) <= 5 else default


def select_paths(payload: Any, paths: list[str]) -> Any:
    """Project a payload down to dotted paths. `results[].id` walks each element."""

    def walk(node: Any, parts: list[str]) -> Any:
        if not parts:
            return node
        head, *rest = parts
        key = head.removesuffix("[]")
        if key != head:
            container = node.get(key) if isinstance(node, dict) else None
            if not isinstance(container, list):
                return None
            return [walk(item, rest) for item in container]
        if isinstance(node, list):
            return [walk(item, parts) for item in node]
        if isinstance(node, dict):
            return walk(node.get(head), rest)
        return None

    if len(paths) == 1:
        return walk(payload, paths[0].split("."))
    return {path: walk(payload, path.split(".")) for path in paths}


def _dumps(value: Any, opts: Opts) -> str:
    """JSON with the indentation the user asked for."""
    return json.dumps(value, ensure_ascii=False, indent=None if opts.compact else 2)


def render(payload: dict[str, Any], opts: Opts) -> str:
    text_limit = None if opts.full_text else opts.text_limit
    fmt = "json" if opts.raw else opts.format
    match fmt:
        case "json":
            return _dumps({k: v for k, v in payload.items() if not k.startswith("__")} if opts.raw else tidy(payload), opts)
        case "jsonl":
            return "\n".join(json.dumps(s if opts.raw else tidy(s), ensure_ascii=False) for s in collect_statuses(payload))
        case "md":
            return format_markdown(payload, text_limit)
        case "urls":
            return format_urls(payload)
        case "media-urls":
            return "\n".join(url for url, _ in media_urls(payload, opts.media_kind, opts.best_variant))
        case _:
            return format_summary(payload, text_limit)

# --------------------------------------------------------------------------- #
# Command execution
# --------------------------------------------------------------------------- #

_LISTING = {"quotes", "statuses", "search", "conversation"}
_ITEM_KEYS = ("results", "trends", "thread", "replies")
_GROUP_READ = "Read"
_GROUP_ACCOUNT = "Account"
_GROUP_DISCOVER = "Discover"
_GROUP_TOOLS = "Tools"


@Parameter(name="*")
@dataclass
class Opts:
    format: Annotated[
        Literal["summary", "json", "jsonl", "md", "urls", "media-urls"],
        Parameter(group="Output", alias="-f"),
    ] = "summary"
    raw: Annotated[bool, Parameter(group="Output", negative="")] = False
    compact: Annotated[bool, Parameter(group="Output", negative="")] = False
    select: Annotated[list[str], Parameter(group="Output", negative_iterable="")] = field(default_factory=list)
    text_limit: Annotated[int, Parameter(group="Output", validator=validators.Number(gte=0))] = 600
    full_text: Annotated[bool, Parameter(group="Output", negative="")] = False
    out: Annotated[pathlib.Path | None, Parameter(group="Output", alias="-o")] = None
    media_kind: Annotated[Literal["all", "photos", "videos"], Parameter(group="Output")] = "all"
    best_variant: Annotated[bool, Parameter(group="Output", negative="--no-best-variant")] = True

    base_url: Annotated[
        str,
        Parameter(group="Network", env_var="TWEETCIDER_API_BASE", show_env_var=True),
    ] = DEFAULT_BASE
    user_agent: Annotated[
        str,
        Parameter(group="Network", env_var="TWEETCIDER_USER_AGENT", show_env_var=True),
    ] = DEFAULT_UA
    timeout: Annotated[
        float,
        Parameter(
            group="Network",
            env_var="TWEETCIDER_TIMEOUT",
            show_env_var=True,
            validator=validators.Number(gt=0),
        ),
    ] = DEFAULT_TIMEOUT
    retries: Annotated[int, Parameter(group="Network", validator=validators.Number(gte=0, lte=10))] = 3
    verbose: Annotated[bool, Parameter(group="Network", alias="-v", negative="")] = False

    pages: Annotated[int, Parameter(group="Paging", validator=validators.Number(gte=1))] = 1
    all_pages: Annotated[bool, Parameter(name="--all", group="Paging", negative="")] = False
    max_items: Annotated[int | None, Parameter(group="Paging", validator=validators.Number(gte=1))] = None
    retry_empty: Annotated[int, Parameter(group="Paging", validator=validators.Number(gte=0, lte=5))] = 2
    count: Annotated[int | None, Parameter(group="Paging")] = None
    cursor: Annotated[str | None, Parameter(group="Paging")] = None

    lang: Annotated[str | None, Parameter(group="Query", alias="-l")] = None
    about_account: Annotated[bool, Parameter(group="Query", negative="")] = False

    cache: Annotated[bool, Parameter(group="Cache", negative="--no-cache")] = True
    cache_ttl: Annotated[
        float,
        Parameter(
            group="Cache",
            env_var="TWEETCIDER_CACHE_TTL",
            show_env_var=True,
            validator=validators.Number(gte=0),
        ),
    ] = 300.0
    concurrency: Annotated[int, Parameter(group="Cache", validator=validators.Number(gte=1, lte=32))] = 4


# One shared all-defaults instance. Cyclopts never mutates it; it binds a fresh
# object per invocation, and ruff forbids a call in a default argument.
DEFAULT_OPTS = Opts()


def looks_empty(payload: Any) -> bool:
    """True when a list container came back empty. The guest session is flaky."""
    if not isinstance(payload, dict) or payload.get("__no_content__"):
        return False
    for key in _ITEM_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return not (value or payload.get("status") or payload.get("user"))
    return False


async def with_empty_retry(fetch, opts: Opts):
    payload = await fetch()
    for attempt in range(max(0, opts.retry_empty)):
        if not looks_empty(payload):
            break
        if opts.verbose:
            print(f"[tweetcider] empty payload, retry {attempt + 1}/{opts.retry_empty}", file=sys.stderr)
        await asyncio.sleep(0.6 * (attempt + 1))
        payload = await fetch()
    return payload


async def paginate(client: Client, path: str, params: dict, opts: Opts, items_key: str = "results") -> dict:
    """Follow cursor.bottom, merging items while keeping the first page's shape."""
    limit = 1000 if opts.all_pages else max(1, opts.pages)
    merged: dict | None = None
    items: list = []
    cursor = params.get("cursor")
    last_status = 200
    fetched = 0
    while fetched < limit:
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        reply = await client.get(path, page_params)
        last_status = reply.status
        payload = reply.payload
        body_code = payload.get("code") if isinstance(payload, dict) else None
        if not 200 <= reply.status < 300 or (isinstance(body_code, int) and not 200 <= body_code < 300):
            message = payload.get("message") if isinstance(payload, dict) else None
            raise TweetciderError(
                f"{path} failed on page {fetched + 1}: HTTP {reply.status}" + (f" ({message})" if message else ""),
                EXIT_API_ERROR,
            )
        if payload is None:
            if reply.status == 204 and merged is None:
                return {"__no_content__": True, "__http_status__": 204}
            if reply.status == 204:
                cursor = None
                break
            raise TweetciderError(f"empty response from {path} (HTTP {reply.status})", EXIT_API_ERROR)
        if not isinstance(payload, dict):
            raise TweetciderError(f"unexpected response shape from {path}")
        if merged is None:
            merged = dict(payload)
        chunk = payload.get(items_key) or []
        items.extend(chunk)
        fetched += 1
        cursor_obj = payload.get("cursor")
        next_cursor = cursor_obj.get("bottom") if isinstance(cursor_obj, dict) else None
        if opts.max_items is not None and len(items) > opts.max_items:
            print(f"note: stopped at --max-items {opts.max_items}; raise it to see more", file=sys.stderr)
            cursor = None
            break
        if not chunk or not next_cursor or next_cursor == cursor:
            cursor = next_cursor
            break
        cursor = next_cursor
        if opts.max_items is not None and len(items) >= opts.max_items:
            break
    if merged is None:
        raise TweetciderError(f"no payload returned from {path}")
    if opts.max_items is not None:
        items = items[: opts.max_items]
    merged[items_key] = items
    cursor_obj = merged.get("cursor")
    if isinstance(cursor_obj, dict):
        merged["cursor"] = {**cursor_obj, "bottom": cursor}
    merged["__http_status__"] = last_status
    if fetched > 1:
        merged["__pages_fetched__"] = fetched
    return merged


def listing_params(opts: Opts, *, lang: bool = True) -> dict:
    params: dict = {}
    if opts.count:
        params["count"] = opts.count
    if opts.cursor:
        params["cursor"] = opts.cursor
    if lang and opts.lang:
        params["lang"] = opts.lang
    return params


async def fetch_payload(
    cmd: str,
    target: str | None,
    opts: Opts,
    *,
    ranking_mode: Literal["likes", "recency"] = "likes",
    since: float | None = None,
    with_replies: bool = False,
    group_threads: bool = False,
    feed: str = "latest",
    result_type: str | None = None,
    src: str | None = None,
    trend_type: str = "trending",
    params: dict | None = None,
) -> Any:
    params = dict(params or {})
    async with aclosing(Client(opts)) as client:
        match cmd:
            case "status" | "thread" | "conversation":
                path = f"/2/{cmd}/{parse_status_id(target or '')}"
                if opts.lang:
                    params["lang"] = opts.lang
                if opts.about_account:
                    params["about_account"] = "1"
                if cmd == "conversation":
                    params["ranking_mode"] = ranking_mode
                    if opts.cursor:
                        params["cursor"] = opts.cursor
            case "quotes":
                path = f"/2/status/{parse_status_id(target or '')}/quotes"
                params = listing_params(opts)
            case "statuses":
                path = f"/2/profile/{parse_handle(target or '')}/statuses"
                params = listing_params(opts)
                if since is not None:
                    params["since"] = int(since / 1000) if since >= 1e12 else int(since)
                if with_replies:
                    params["with_replies"] = "1"
                if group_threads:
                    params["groupthreads"] = "1"
            case "search":
                path = "/2/search"
                params = listing_params(opts)
                params["q"] = target or ""
                params["feed"] = feed
            case "typeahead":
                path = "/2/typeahead"
                params["q"] = target or ""
                if result_type:
                    params["result_type"] = result_type
                if src:
                    params["src"] = src
            case "trends":
                path = "/2/trends"
                params = {"type": trend_type, "count": opts.count or 20}
            case "get":
                path = target or ""
            case _:
                raise TweetciderError(f"unknown command {cmd!r}", EXIT_USAGE)
        if cmd in _LISTING:
            key = "replies" if cmd == "conversation" else "results"
            return await with_empty_retry(partial(paginate, client, path, params, opts, key), opts)

        async def one():
            reply = await client.get(path, params)
            payload = reply.payload if reply.payload is not None else {}
            if isinstance(payload, dict):
                payload = dict(payload)
                payload["__http_status__"] = reply.status
            return payload

        return await with_empty_retry(one, opts)


def format_payload(payload: Any, opts: Opts) -> str:
    if opts.select:
        return _dumps(select_paths(payload if opts.raw else tidy(payload), opts.select), opts)
    if not isinstance(payload, dict):
        return _dumps(payload, opts)
    return render(payload, opts)


def exit_code(payload: Any) -> int:
    if isinstance(payload, dict):
        if payload.get("__no_content__"):
            return EXIT_NO_CONTENT
        http_status = payload.get("__http_status__")
        if isinstance(http_status, int) and not 200 <= http_status < 300:
            return EXIT_API_ERROR
        code = payload.get("code")
        if isinstance(code, int) and not 200 <= code < 300:
            return EXIT_API_ERROR
    return EXIT_OK


def write_output(text: str, opts: Opts) -> None:
    if opts.out:
        try:
            opts.out.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            raise TweetciderError(f"cannot write {opts.out}: {exc}") from exc
        print(f"wrote {opts.out}", file=sys.stderr)
    else:
        print(text)


async def run(cmd: str, target: str | None, opts: Opts, **kwargs) -> int:
    payload = await fetch_payload(cmd, target, opts, **kwargs)
    write_output(format_payload(payload, opts), opts)
    return exit_code(payload)

# --------------------------------------------------------------------------- #
# mine: score a conversation's posts against a query with Cloudflare clef-flash
# --------------------------------------------------------------------------- #

# One 2-way choice per post. Labelled tests on two conversations: a separate slop or AI-written stage did no better
# than chance (the 9B model reads fluent AI replies as substantive), and other wordings ranked no better than this one.
_CLASSES = {
    "useful": "adds information, evidence, a correction, a useful link or a substantive view that helps answer "
    "the query about original_post",
    "chatter": "agreement, jokes, greetings, reactions or off-topic remarks with no substance",
}
_CLEF_MAX_QUESTIONS = 64
_CLEF_BAND = 0.15  # posts within this of --threshold are re-scored alone
_CLEF_QUESTION_TOKENS = 90  # measured ~84 per 4-option choice; 2 options is cheaper, so this is safe
_CLEF_FIXED_TOKENS = 100  # measured ~77 of template overhead per request, rounded up
_CLEF_BUDGET = {"cloudflare": 24000, "custom": 8000}  # clef-flash context is 24,576; a custom server's batch is unknown
_TOKENIZER = pathlib.Path(__file__).with_name("clef-flash-tokenizer.json")
@cache
def _load_tokenizer() -> Tokenizer | None:
    return Tokenizer.from_file(str(_TOKENIZER)) if _TOKENIZER.exists() else None


def count_tokens(value: Any) -> int:
    """Tokens clef-flash will see for this JSON value. Without the bundled tokenizer, 1 per char is the safe bound."""
    text = json.dumps(value, ensure_ascii=False)
    tokenizer = _load_tokenizer()
    return len(tokenizer.encode(text).ids) if tokenizer else len(text)


def clef_credentials(key_file: pathlib.Path | None) -> tuple[str, str]:
    """(account id, token) from CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_API_TOKEN, else the key file's two lines."""
    account, token = os.environ.get("CLOUDFLARE_ACCOUNT_ID"), os.environ.get("CLOUDFLARE_API_TOKEN")
    if not (account and token) and key_file:
        try:
            account, token = [*key_file.read_text().split(), "", ""][:2]
        except OSError as exc:
            raise TweetciderError(f"cannot read {key_file}: {exc}", EXIT_USAGE) from exc
    if not (account and token):
        raise TweetciderError(
            "mine needs a decider: set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN, pass --cf-key-file "
            "(line 1 account id, line 2 token), or point --decider-url at a local clef-flash /v1/systemone.",
            EXIT_USAGE,
        )
    return account, token


def _candidate(status: dict[str, Any]) -> dict[str, Any]:
    return {"author": (status.get("author") or {}).get("screen_name"), "text": _clip(status.get("text") or "", 800)}


def clef_batches(statuses: list[dict], fixed: int, budget: int, max_posts: int = _CLEF_MAX_QUESTIONS) -> list[list[dict]]:
    """Pack posts into requests of at most `budget` estimated tokens and 64 questions.

    Cost is fixed + sum(post + question), so the fixed part is paid once per request: fewer, fuller requests are
    cheaper. Longest-first next-fit keeps requests full; order does not matter because results are re-sorted.
    """
    costed = sorted(((count_tokens(_candidate(s)) + _CLEF_QUESTION_TOKENS, s) for s in statuses), key=lambda c: -c[0])
    batches: list[list[dict]] = []
    used = budget
    for cost, status in costed:
        if fixed + cost > budget:
            raise TweetciderError(f"decider budget {budget} is too small for the query and a post (need at least {fixed + cost})", EXIT_USAGE)
        if used + cost > budget or len(batches[-1]) == max_posts:
            batches.append([])
            used = fixed
        batches[-1].append(status)
        used += cost
    return batches


async def clef_score(
    client: Client, url: str, token: str | None, header: dict, batch: list[dict]
) -> tuple[dict[str, float], int]:
    """One request: {post id: P(useful)} and the input tokens billed. Posts go by short index, not id."""
    criteria = dict.fromkeys(_CLASSES)
    body = {
        "model": "clef-flash",
        "state": {**header, "classes": _CLASSES, "posts": {str(i): _candidate(s) for i, s in enumerate(batch)}},
        "questions": {
            f"c{i}": {"type": "choice", "instructions": f"Classify post {i}", "criteria": criteria}
            for i in range(len(batch))
        },
    }
    try:
        response = await client.session.post(
            url, json=body, headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=client.timeout
        )
        raw = await read_body(response)
    except (niquests.exceptions.RequestException, OSError) as exc:
        raise TweetciderError(f"network error calling the decider: {exc}")
    payload = decode(raw or b"", response.status_code or 0, url)
    result = payload.get("result", payload) if isinstance(payload, dict) else None
    if not isinstance(result, dict) or "answers" not in result:
        errors = payload.get("errors") or payload.get("error") if isinstance(payload, dict) else payload
        raise TweetciderError(f"decider failed (HTTP {response.status_code}): {errors}", EXIT_API_ERROR)
    try:
        scores = {s["id"]: float(result["answers"][f"c{i}"]["probabilities"]["useful"]) for i, s in enumerate(batch)}
    except (KeyError, TypeError, ValueError) as exc:
        raise TweetciderError(f"decider answered in an unexpected shape: missing {exc}", EXIT_API_ERROR) from exc
    return scores, int(result.get("usage", {}).get("input_tokens", 0))


@dataclass
class Decider:
    client: Client
    url: str
    token: str | None
    header: dict
    budget: int
    semaphore: asyncio.Semaphore
    requests: int = 0
    input_tokens: int = 0

    async def score(self, batch: list[dict]) -> tuple[dict[str, float], int]:
        async with self.semaphore:
            return await clef_score(self.client, self.url, self.token, self.header, batch)

    async def classify(self, posts: list[dict], max_posts: int = _CLEF_MAX_QUESTIONS) -> dict[str, float]:
        fixed = _CLEF_FIXED_TOKENS + count_tokens(self.header)
        batches = clef_batches(posts, fixed, self.budget, max_posts)
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self.score(batch)) for batch in batches]
        self.requests += len(tasks)
        self.input_tokens += sum(task.result()[1] for task in tasks)
        return {pid: p for task in tasks for pid, p in task.result()[0].items()}


async def mine_payload(
    target: str,
    query: str,
    opts: Opts,
    *,
    key_file: pathlib.Path | None,
    threshold: float,
    limit: int | None,
    decider_url: str | None = None,
    budget: int | None = None,
) -> dict:
    account, token = ("", None) if decider_url else clef_credentials(key_file)
    url = decider_url or CLEF_URL.format(account)
    budget = budget or _CLEF_BUDGET["custom" if decider_url else "cloudflare"]
    # Same URLs as `conversation --all` / `quotes --all`, so those reads hit the response cache afterwards.
    every = dataclasses.replace(opts, all_pages=True)
    conv = await fetch_payload("conversation", target, every)
    if exit_code(conv) != EXIT_OK or not isinstance(conv.get("status"), dict):
        raise TweetciderError(f"cannot fetch conversation: HTTP {conv.get('code')} ({conv.get('message')})", EXIT_API_ERROR)
    focal = conv["status"]
    warning = {}
    try:
        quoted = (await fetch_payload("quotes", target, every)).get("results") or []
    except TweetciderError as exc:  # quotes 404 often on the public instance
        quoted, warning = [], {"warning": f"quotes unavailable: {exc}"}
    pool = chain(conv.get("thread") or [], conv.get("replies") or [], quoted)
    seen = {focal.get("id")}
    candidates = [s for s in pool if isinstance(s, dict) and s.get("text") and not (s["id"] in seen or seen.add(s["id"]))]

    # Exact repeats (after dropping mentions and links) are copy-paste bots; keep the first, skip the rest.
    firsts: set[str] = set()
    duplicates, fresh = [], []
    for status in candidates:
        key = " ".join(re.sub(r"https?://\S+|@\w+", "", status["text"]).lower().split())
        (duplicates if len(key) >= 15 and key in firsts else fresh).append(status)
        firsts.add(key)

    header = {"query": query, "original_post": _candidate(focal)}
    async with aclosing(Client(opts)) as client:
        decider = Decider(client, url, token, header, budget, asyncio.Semaphore(opts.concurrency))
        try:
            # Re-score posts near the cutoff alone because scores depend on batchmates.
            relevance = await decider.classify(fresh) if fresh else {}
            ranked = sorted(relevance.values(), reverse=True)
            line = ranked[limit - 1] if limit and len(ranked) > limit and ranked[limit - 1] > threshold else threshold
            band = [s for s in fresh if abs(relevance[s["id"]] - line) < _CLEF_BAND]
            if band:
                relevance.update(await decider.classify(band, max_posts=1))
        except* TweetciderError as errors:
            raise errors.exceptions[0] from None
    kept = [s for s in fresh if relevance[s["id"]] >= threshold]
    results = [
        # A quote tweet's embedded quote is the focal post, already shown first.
        {**{k: v for k, v in s.items() if not (k == "quote" and (v or {}).get("id") == focal.get("id"))},
         "relevance": relevance[s["id"]]}
        for s in sorted(kept, key=lambda s: -relevance[s["id"]])[:limit]
    ]
    mined = {
        "query": query,
        "shown": len(results),
        "relevant": len(kept),
        "scored": len(fresh),
        "duplicates": len(duplicates),
        "threshold": threshold,
        "rescored_alone": len(band),
        "requests": decider.requests,
        "input_tokens": decider.input_tokens,
        **warning,
    }
    return {"code": 200, "status": focal, "results": results, "mined": mined}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

app = App(
    name="tweetcider",
    help="Read X/Twitter posts, threads and timelines through the FxTwitter API (no key, no login).",
    version=__version__,
)


@app.command(group=_GROUP_READ)
async def status(target: str, opts: Opts = DEFAULT_OPTS) -> int:
    """One post: text, stats, media, quoted post. TARGET is an id or permalink."""
    return await run("status", target, opts)


@app.command(group=_GROUP_READ)
async def thread(target: str, opts: Opts = DEFAULT_OPTS) -> int:
    """A post plus the author's self-reply chain."""
    return await run("thread", target, opts)


@app.command(group=_GROUP_READ)
async def conversation(
    target: str,
    opts: Opts = DEFAULT_OPTS,
    ranking_mode: Annotated[Literal["likes", "recency"], Parameter(group="Query")] = "likes",
) -> int:
    """A thread plus replies from other accounts."""
    return await run("conversation", target, opts, ranking_mode=ranking_mode)


@app.command(group=_GROUP_READ)
async def quotes(target: str, opts: Opts = DEFAULT_OPTS) -> int:
    """Posts quoting a post."""
    return await run("quotes", target, opts)


@app.command(group=_GROUP_READ)
async def mine(
    target: str,
    query: Annotated[str, Parameter(allow_leading_hyphen=True)],
    opts: Opts = DEFAULT_OPTS,
    threshold: Annotated[float, Parameter(group="Decider", validator=validators.Number(gte=0, lte=1))] = 0.5,
    limit: Annotated[int | None, Parameter(group="Decider", validator=validators.Number(gte=1))] = None,
    cf_key_file: Annotated[pathlib.Path | None, Parameter(group="Decider", env_var="TWEETCIDER_CF_KEY_FILE")] = None,
    decider_url: Annotated[str | None, Parameter(group="Decider", env_var="TWEETCIDER_DECIDER_URL")] = None,
    decider_budget: Annotated[int | None, Parameter(group="Decider", validator=validators.Number(gte=500))] = None,
) -> int:
    """Mine a post's thread, replies and quote tweets for what answers QUERY. Always returns the post itself."""
    payload = await mine_payload(
        target, query, opts, key_file=cf_key_file, threshold=threshold, limit=limit,
        decider_url=decider_url, budget=decider_budget,
    )
    write_output(format_payload(payload, opts), opts)
    return EXIT_OK


@app.command(group=_GROUP_ACCOUNT)
async def statuses(
    target: str,
    opts: Opts = DEFAULT_OPTS,
    since: Annotated[float | None, Parameter(group="Query")] = None,
    with_replies: Annotated[bool, Parameter(group="Query", negative="")] = False,
    group_threads: Annotated[bool, Parameter(group="Query", negative="")] = False,
) -> int:
    """An account's recent posts. --since takes unix seconds; exit 4 means nothing newer."""
    return await run(
        "statuses", target, opts, since=since, with_replies=with_replies, group_threads=group_threads
    )


@app.command(group=_GROUP_DISCOVER)
async def search(
    target: Annotated[str, Parameter(allow_leading_hyphen=True)],
    opts: Opts = DEFAULT_OPTS,
    feed: Annotated[str, Parameter(group="Query")] = "latest",
) -> int:
    """Search posts (X operators supported). Unreliable on the public instance."""
    return await run("search", target, opts, feed=feed)


@app.command(group=_GROUP_DISCOVER)
async def typeahead(
    target: Annotated[str, Parameter(allow_leading_hyphen=True)],
    opts: Opts = DEFAULT_OPTS,
    result_type: Annotated[str | None, Parameter(group="Query")] = None,
    src: Annotated[str | None, Parameter(group="Query")] = None,
) -> int:
    """Autocomplete users, topics and events. Reliable way to find a handle."""
    return await run("typeahead", target, opts, result_type=result_type, src=src)


@app.command(group=_GROUP_DISCOVER)
async def trends(
    opts: Opts = DEFAULT_OPTS, trend_type: Annotated[str, Parameter(name="--type", group="Query")] = "trending"
) -> int:
    """Trending topics right now."""
    return await run("trends", None, opts, trend_type=trend_type)


@app.command(group=_GROUP_TOOLS)
async def get(
    path: Annotated[str, Parameter(allow_leading_hyphen=True)],
    opts: Opts = DEFAULT_OPTS,
    param: Annotated[list[str] | None, Parameter(name="--param", requires_equals=True, negative="")] = None,
) -> int:
    """Call an arbitrary API path, e.g. `/2/status/20 --param=lang=ja`."""
    params = {key: value for key, sep, value in (entry.partition("=") for entry in param or []) if sep}
    return await run("get", path, opts, params=params)


@app.command(group=_GROUP_TOOLS)
async def fetch(
    urls: Annotated[list[str], Parameter(negative_iterable="")], opts: Opts = DEFAULT_OPTS
) -> int:
    """Fetch several posts concurrently. Results print in input order."""
    if opts.out and len(urls) > 1:
        raise TweetciderError("--out supports one result at a time", EXIT_USAGE)
    semaphore = asyncio.Semaphore(max(1, opts.concurrency))

    async def one(url: str):
        async with semaphore:
            return await fetch_payload("status", url, opts)

    results = await asyncio.gather(*(one(url) for url in urls), return_exceptions=True)
    failed = False
    wanted_json = opts.raw or opts.format == "json"
    if wanted_json:
        rows = []
        for url, result in zip(urls, results):
            if isinstance(result, BaseException):
                failed = True
                rows.append({"input": url, "error": str(result)})
            else:
                if exit_code(result) != EXIT_OK:
                    failed = True
                value = result if opts.raw else tidy(result)
                rows.append(select_paths(value, opts.select) if opts.select and exit_code(result) == EXIT_OK else value)
        write_output(_dumps(rows, opts), opts)
    else:
        blocks = []
        for url, result in zip(urls, results):
            if isinstance(result, BaseException):
                failed = True
                blocks.append(json.dumps({"input": url, "error": str(result)}, ensure_ascii=False) if opts.format == "jsonl"
                              else f"== {url}\n!! error: {result}")
            elif exit_code(result) != EXIT_OK:
                failed = True
                blocks.append(json.dumps({"input": url, "error": result}, ensure_ascii=False) if opts.format == "jsonl"
                              else f"== {url}\n{format_payload(result, opts)}")
            else:
                blocks.append(format_payload(result, opts))
        write_output(("\n" if opts.format == "jsonl" else "\n\n").join(blocks), opts)
    return EXIT_PARTIAL if failed else EXIT_OK


@app.command(group=_GROUP_TOOLS)
async def download(
    target: str,
    opts: Opts = DEFAULT_OPTS,
    out_dir: Annotated[pathlib.Path, Parameter(group="Query")] = DEFAULT_MEDIA_DIR,
    dry_run: Annotated[bool, Parameter(group="Query", negative="")] = False,
) -> int:
    """Save media from a post to disk."""
    async with aclosing(Client(opts)) as client:
        reply = await client.get(f"/2/status/{parse_status_id(target)}")
        payload = reply.payload
        if not isinstance(payload, dict) or not 200 <= reply.status < 300 or exit_code(payload) != EXIT_OK:
            message = payload.get("message") if isinstance(payload, dict) else None
            raise TweetciderError(f"cannot fetch post: HTTP {reply.status}" + (f" ({message})" if message else ""),
                                  EXIT_API_ERROR)
        pairs = media_urls(payload, opts.media_kind, opts.best_variant)
        if not pairs:
            print("no media found", file=sys.stderr)
            return EXIT_OK
        if not dry_run:
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise TweetciderError(f"cannot create {out_dir}: {exc}") from exc
        failed = False
        for url, name in pairs:
            destination = out_dir / name
            if dry_run:
                print(f"{url} -> {destination}")
                continue
            try:
                response = await client.session.get(url, timeout=opts.timeout)
                raw = await read_body(response)
            except (niquests.exceptions.RequestException, OSError) as exc:
                print(f"failed {url}: {exc}", file=sys.stderr)
                failed = True
                continue
            status = response.status_code or 0
            if not 200 <= status < 300 or raw is None:
                print(f"failed {url}: HTTP {status or 'unknown'}", file=sys.stderr)
                failed = True
                continue
            try:
                destination.write_bytes(raw)
            except OSError as exc:
                print(f"failed {destination}: {exc}", file=sys.stderr)
                failed = True
                continue
            print(f"saved {destination}")
    return EXIT_PARTIAL if failed else EXIT_OK


def main() -> int:
    try:
        app()
    except TweetciderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
