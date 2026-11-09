"""Tests for the TweetCider script. Run with `.venv/bin/python -m unittest`."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from unittest import mock

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "skills" / "tweetcider" / "scripts" / "tweetcider.py"
_spec = importlib.util.spec_from_file_location("tweetcider", _SCRIPT)
assert _spec and _spec.loader
tc = importlib.util.module_from_spec(_spec)
sys.modules["tweetcider"] = tc
_spec.loader.exec_module(tc)


class TestParseStatusId(unittest.TestCase):
    def test_bare_id(self):
        self.assertEqual(tc.parse_status_id("20"), "20")

    def test_whitespace(self):
        self.assertEqual(tc.parse_status_id("  20  "), "20")

    def test_x_permalink(self):
        self.assertEqual(tc.parse_status_id("https://x.com/jack/status/20"), "20")

    def test_twitter_permalink_with_query(self):
        self.assertEqual(tc.parse_status_id("https://twitter.com/jack/status/20?s=20&t=abc"), "20")

    def test_fxembed_link(self):
        self.assertEqual(tc.parse_status_id("https://fxtwitter.com/jack/status/20"), "20")

    def test_statuses_plural(self):
        self.assertEqual(tc.parse_status_id("x.com/i/statuses/20"), "20")

    def test_mobile_link(self):
        self.assertEqual(tc.parse_status_id("mobile.twitter.com/jack/status/20/photo/1"), "20")

    def test_media_suffix_permalink(self):
        self.assertEqual(tc.parse_status_id("https://x.com/jack/status/20.jpg"), "20")
        self.assertEqual(tc.parse_status_id("https://d.fxtwitter.com/jack/status/20.MP4"), "20")

    def test_empty_raises_usage(self):
        with self.assertRaises(tc.TweetciderError) as ctx:
            tc.parse_status_id("")
        self.assertEqual(ctx.exception.code, tc.EXIT_USAGE)

    def test_garbage_raises_usage(self):
        with self.assertRaises(tc.TweetciderError) as ctx:
            tc.parse_status_id("not a url")
        self.assertEqual(ctx.exception.code, tc.EXIT_USAGE)


class TestParseHandle(unittest.TestCase):
    def test_at_handle(self):
        self.assertEqual(tc.parse_handle("@jack"), "jack")

    def test_bare_handle(self):
        self.assertEqual(tc.parse_handle("jack"), "jack")

    def test_profile_url(self):
        self.assertEqual(tc.parse_handle("https://x.com/jack"), "jack")

    def test_profile_url_with_query(self):
        self.assertEqual(tc.parse_handle("https://x.com/jack?lang=en"), "jack")

    def test_bare_domain(self):
        self.assertEqual(tc.parse_handle("x.com/jack/"), "jack")

    def test_numeric_becomes_id(self):
        self.assertEqual(tc.parse_handle("12"), "id:12")

    def test_explicit_id_prefix(self):
        self.assertEqual(tc.parse_handle("id:12"), "id:12")

    def test_bad_id_prefix(self):
        with self.assertRaises(tc.TweetciderError):
            tc.parse_handle("id:abc")

    def test_invalid_handle(self):
        with self.assertRaises(tc.TweetciderError) as ctx:
            tc.parse_handle("way-too-long-handle-name")
        self.assertEqual(ctx.exception.code, tc.EXIT_USAGE)


class TestSelectPaths(unittest.TestCase):
    payload: ClassVar[dict] = {
        "status": {"text": "hi", "author": {"screen_name": "jack"}},
        "results": [
            {"id": "1", "text": "a"},
            {"id": "2", "text": "b"},
        ],
    }

    def test_single_path_returns_value(self):
        self.assertEqual(tc.select_paths(self.payload, ["status.text"]), "hi")

    def test_multiple_paths_return_mapping(self):
        got = tc.select_paths(self.payload, ["status.text", "status.author.screen_name"])
        self.assertEqual(got, {"status.text": "hi", "status.author.screen_name": "jack"})

    def test_list_wildcard(self):
        self.assertEqual(tc.select_paths(self.payload, ["results[].id"]), ["1", "2"])

    def test_missing_path_is_none(self):
        self.assertIsNone(tc.select_paths(self.payload, ["nope.deep"]))

    def test_wildcard_on_non_list_is_none(self):
        self.assertIsNone(tc.select_paths(self.payload, ["status[]"]))

    def test_literal_brackets_fall_through_list(self):
        self.assertEqual(tc.select_paths(self.payload, ["results.text"]), ["a", "b"])


class TestHeuristics(unittest.TestCase):
    def test_looks_empty_true(self):
        self.assertTrue(tc.looks_empty({"code": 200, "results": []}))

    def test_looks_empty_false_with_results(self):
        self.assertFalse(tc.looks_empty({"results": [{"id": "1"}]}))

    def test_looks_empty_false_with_status(self):
        self.assertFalse(tc.looks_empty({"thread": [], "status": {"id": "1"}}))

    def test_looks_empty_false_when_key_absent(self):
        self.assertFalse(tc.looks_empty({"user": {"screen_name": "jack"}}))

    def test_looks_empty_ignores_no_content(self):
        self.assertFalse(tc.looks_empty({"__no_content__": True, "results": []}))

    def test_exit_code_ok(self):
        self.assertEqual(tc.exit_code({"code": 200}), tc.EXIT_OK)

    def test_exit_code_api_error(self):
        self.assertEqual(tc.exit_code({"code": 404}), tc.EXIT_API_ERROR)
        self.assertEqual(tc.exit_code({"__http_status__": 503}), tc.EXIT_API_ERROR)

    def test_non_json_http_error(self):
        with self.assertRaises(tc.TweetciderError) as ctx:
            tc.decode(b"service unavailable", 503, "http://localhost")
        self.assertEqual(ctx.exception.code, tc.EXIT_API_ERROR)

    def test_exit_code_no_content(self):
        self.assertEqual(tc.exit_code({"__no_content__": True}), tc.EXIT_NO_CONTENT)

    def test_exit_code_non_dict(self):
        self.assertEqual(tc.exit_code(["a"]), tc.EXIT_OK)

    def test_extension_from_url(self):
        self.assertEqual(tc._extension_from_url("https://x.com/a.MP4", "jpg"), "mp4")

    def test_extension_from_query(self):
        url = "https://video.twimg.com/a?tag=29&format=webm"
        self.assertEqual(tc._extension_from_url(url, "mp4"), "webm")

    def test_extension_default(self):
        self.assertEqual(tc._extension_from_url("https://x.com/media", "jpg"), "jpg")

    def test_clip_short(self):
        self.assertEqual(tc._clip("hello", 10), "hello")

    def test_clip_long(self):
        self.assertEqual(tc._clip("hello world", 5), "hello world")  # cutting 6 chars saves nothing
        self.assertEqual(tc._clip("x" * 300, 50), "x" * 50 + "... [+250 chars, --full-text]")

    def test_clip_no_limit(self):
        self.assertEqual(tc._clip("hello", None), "hello")


class TestMedia(unittest.TestCase):
    payload: ClassVar[dict] = {
        "status": {
            "type": "status",
            "id": "99",
            "text": "media",
            "media": {
                "photos": [{"url": "https://pbs.twimg.com/a.jpg", "width": 1, "height": 2}],
                "videos": [
                    {
                        "url": "https://video.twimg.com/low.mp4",
                        "formats": [
                            {"bitrate": 100, "url": "https://video.twimg.com/low.mp4"},
                            {"bitrate": 900, "url": "https://video.twimg.com/high.mp4"},
                        ],
                    }
                ],
                "external": {"type": "gif", "url": "https://example.com/x.gif"},
            },
        }
    }

    def test_photos_and_videos(self):
        pairs = tc.media_urls(self.payload, "all", best=True)
        urls = [url for url, _ in pairs]
        self.assertIn("https://pbs.twimg.com/a.jpg", urls)
        self.assertIn("https://video.twimg.com/high.mp4", urls)
        self.assertIn("https://example.com/x.gif", urls)

    def test_best_variant_off(self):
        pairs = tc.media_urls(self.payload, "videos", best=False)
        self.assertEqual(pairs[0][0], "https://video.twimg.com/low.mp4")

    def test_photos_only(self):
        kinds = {name.rsplit(".", 1)[-1] for _, name in tc.media_urls(self.payload, "photos")}
        self.assertEqual(kinds, {"jpg"})

    def test_names_include_status_id(self):
        pairs = tc.media_urls(self.payload, "photos")
        self.assertTrue(pairs[0][1].startswith("99_photo1"))

    def test_names_from_the_api_cannot_leave_the_folder(self):
        photo = {"url": "https://pbs.twimg.com/media/x?format=../../../.bashrc"}
        evil = {"status": {"type": "status", "id": "../../etc", "text": "", "media": {"photos": [photo]}}}
        self.assertEqual(tc.media_urls(evil, "photos")[0][1], "unknown_photo1.jpg")


class TestRender(unittest.TestCase):
    def opts(self, **kwargs):
        return tc.Opts(**kwargs)

    def test_summary_of_status(self):
        payload = {
            "code": 200,
            "status": {
                "type": "status",
                "id": "20",
                "text": "just setting up my twttr",
                "author": {"screen_name": "jack", "name": "jack"},
                "created_timestamp": 1142974214,
                "likes": 1,
                "url": "https://x.com/jack/status/20",
            },
        }
        text = tc.format_payload(payload, self.opts())
        self.assertEqual(text, "@jack · 2006-03-21 20:50 UTC · 1 like\n  | just setting up my twttr\n"
                               "  https://x.com/jack/status/20")

    def test_summary_tombstone(self):
        payload = {"status": {"type": "tombstone", "reason": "deleted", "message": "gone"}}
        text = tc.format_payload(payload, self.opts())
        self.assertIn("(unavailable: deleted)", text)

    def test_summary_thread_marks_focal_without_repeating_it(self):
        focal = {"id": "1", "text": "focal text"}
        reply = {"id": "2", "text": "reply text"}
        text = tc.format_summary({"status": focal, "thread": [focal.copy(), reply]}, None)
        self.assertEqual(text.count("focal text"), 1)
        self.assertIn("== thread · 2 posts\n▶ ", text)
        self.assertIn("reply text", text)

    def test_summary_merges_focal_and_thread_copies(self):
        focal = {"id": "1", "text": "original", "author": {"screen_name": "alice"}}
        thread_post = {"id": "1", "text": "updated", "author": {"name": "Alice Smith"}, "likes": 42}
        text = tc.format_summary({"status": focal, "thread": [thread_post]}, None)
        self.assertIn("original", text)
        self.assertNotIn("updated", text)
        self.assertIn("@alice (Alice Smith)", text)
        self.assertIn("42 likes", text)

    def test_thread_formats_merge_focal_copy(self):
        focal = {"type": "status", "id": "1", "text": "focal", "author": {"screen_name": "a"}}
        thread_post = {"type": "status", "id": "1", "text": "old", "likes": 42}
        reply = {"type": "status", "id": "2", "text": "reply"}
        payload = {"status": focal, "thread": [thread_post, reply]}
        rows = [json.loads(line) for line in tc.format_payload(payload, self.opts(format="jsonl")).splitlines()]
        self.assertEqual([row["id"] for row in rows], ["1", "2"])
        self.assertEqual(rows[0]["likes"], 42)
        text = tc.format_payload(payload, self.opts(format="md"))
        self.assertEqual(text.count("focal"), 1)
        self.assertNotIn("old", text)
        self.assertIn("42 likes", text)

    def test_conversation_is_a_reply_tree_without_repeats(self):
        focal = {"type": "status", "id": "1", "text": "focal", "author": {"screen_name": "op"}, "replies": 9}
        reply = lambda i, to, who, text, **kw: {
            "type": "status", "id": i, "text": text, "author": {"screen_name": who},
            "replying_to": {"screen_name": "x", "status": to}, **kw,
        }
        replies = [
            reply("2", "1", "a", "@op first", raw_text={"display_text_range": [4, 9]}, quote=focal),
            reply("3", "1", "b", "second"),
            reply("4", "2", "op", "@a thanks", raw_text={"display_text_range": [3, 9]}),
        ]
        text = tc.format_summary({"status": focal, "replies": replies}, None)
        lines = text.splitlines()
        self.assertIn("== replies · 3 of 9", lines)
        self.assertIn("  ↳ @op · -", lines)  # 4 sits under 2, not under 3
        self.assertLess(lines.index("  ↳ @op · -"), lines.index("@b · -"))
        self.assertIn("  | first", lines)  # the reply's leading @mention is dropped
        self.assertIn("  quoting @op, shown above", lines)
        self.assertNotIn("replying to", text)  # every parent is on screen
        quotes = {"results": [{"type": "status", "id": str(i), "text": "q", "quote": {**focal, "text": "big post"}}
                              for i in (5, 6)]}
        for fmt in ("summary", "md"):
            self.assertEqual(tc.format_payload(quotes, tc.Opts(format=fmt)).count("big post"), 1)

    def test_tidy_json(self):
        post = {"type": "status", "id": "2", "text": "t", "source": None, "media": {}, "raw_text": {"text": "t"},
                "created_at": "Fri Nov 06 22:50:41 +0000 2026",
                "author": {"screen_name": "a", "banner_url": "b", "verification": {"verified": True}},
                "quote": {"type": "status", "id": "1", "url": "u1", "text": "focal"}}
        out = tc.tidy({"status": {"type": "status", "id": "1", "url": "u1", "text": "focal"}, "results": [post]})
        self.assertEqual(out["results"][0], {"type": "status", "id": "2", "text": "t",
                                             "created_at": "2026-11-06T22:50:41Z",
                                             "author": {"screen_name": "a", "verified": True},
                                             "quote": {"id": "1", "url": "u1", "shown_above": True}})

    def test_no_content_message(self):
        text = tc.format_payload({"__no_content__": True}, self.opts())
        self.assertIn("204 No Content", text)

    def test_json_drops_private_keys(self):
        payload = {"code": 200, "__http_status__": 200, "status": {"id": "1"}}
        out = json.loads(tc.format_payload(payload, self.opts(format="json")))
        self.assertNotIn("__http_status__", out)

    def test_compact_json_is_one_line(self):
        payload = {"code": 200, "status": {"id": "1"}}
        out = tc.format_payload(payload, self.opts(format="json", compact=True))
        self.assertEqual(len(out.splitlines()), 1)

    def test_jsonl_one_object_per_status(self):
        payload = {"results": [{"type": "status", "id": "1", "text": "a"}, {"type": "status", "id": "2", "text": "b"}]}
        out = tc.format_payload(payload, self.opts(format="jsonl"))
        self.assertEqual(len(out.splitlines()), 2)

    def test_markdown(self):
        payload = {"status": {"type": "status", "id": "1", "text": "hi", "author": {"screen_name": "jack", "url": "https://x.com/jack"}, "url": "https://x.com/jack/status/1"}}
        out = tc.format_payload(payload, self.opts(format="md"))
        self.assertEqual(out, "> hi\n\n**jack** · [@jack](https://x.com/jack/status/1) · -\n")

    def test_urls(self):
        payload = {"status": {"type": "status", "id": "1", "text": "hi", "url": "https://x.com/jack/status/1"}}
        self.assertEqual(tc.format_payload(payload, self.opts(format="urls")), "https://x.com/jack/status/1")

    def test_media_urls_format(self):
        payload = {"status": {"type": "status", "id": "1", "text": "hi", "media": {"photos": [{"url": "https://pbs.twimg.com/a.jpg"}]}}}
        self.assertEqual(tc.format_payload(payload, self.opts(format="media-urls")), "https://pbs.twimg.com/a.jpg")

    def test_select_overrides_summary(self):
        payload = {"status": {"id": "1", "text": "hi"}}
        out = json.loads(tc.format_payload(payload, self.opts(select=["status.text"])))
        self.assertEqual(out, "hi")

    def test_full_text_disables_clip(self):
        long_text = "x" * 1000
        payload = {"status": {"type": "status", "id": "1", "text": long_text, "author": {"screen_name": "a"}}}
        clipped = tc.format_payload(payload, self.opts())
        full = tc.format_payload(payload, self.opts(full_text=True))
        self.assertIn("... [+400 chars, --full-text]", clipped)
        self.assertNotIn("[+400 chars", full)

    def test_grouped_thread(self):
        payload = {
            "results": [
                {
                    "type": "thread",
                    "conversation_id": "5",
                    "truncated": False,
                    "statuses": [
                        {"type": "status", "id": "1", "text": "one", "author": {"screen_name": "a"}},
                        {"type": "status", "id": "2", "text": "two", "author": {"screen_name": "a"}},
                    ],
                }
            ]
        }
        text = tc.format_payload(payload, self.opts())
        self.assertIn("thread 5 · 2 posts", text)
        self.assertIn("one", text)
        self.assertIn("two", text)


class TestCache(unittest.TestCase):
    def test_roundtrip_and_ttl(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"TWEETCIDER_CACHE_DIR": tmp}):
            cache = tc.Cache(60)
            self.assertIsNone(cache.get("u"))
            cache.put("u", 200, {"code": 200, "status": {"id": "1"}})
            self.assertEqual(cache.get("u"), (200, {"code": 200, "status": {"id": "1"}}))
            cache.close()
            again = tc.Cache(60)
            self.assertIsNotNone(again.get("u"))
            again.close()

    def test_zero_ttl_disables(self):
        cache = tc.Cache(0)
        self.assertIsNone(cache.db)
        self.assertIsNone(cache.get("u"))
        cache.put("u", 200, {})
        cache.close()

    def test_expired_entry_missing(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"TWEETCIDER_CACHE_DIR": tmp}):
            cache = tc.Cache(60)
            cache.put("u", 200, {"code": 200})
            cache.ttl = -1  # force expiry
            self.assertIsNone(cache.get("u"))
            cache.close()


class _Handler(BaseHTTPRequestHandler):
    routes: ClassVar[dict[str, tuple[int, dict]]] = {}

    def do_POST(self):  # fake decider: "useful" = 0.85; "mid" = 0.55 alone, 0.45 batched; else 0.05
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if type(self).busy:  # Cloudflare's "capacity temporarily exceeded"
            type(self).busy -= 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        type(self).calls.append(len(body["questions"]))
        answers = {}
        for qid in body["questions"]:
            text = body["state"]["posts"][qid[1:]]["text"]
            p = (0.55 if len(body["questions"]) == 1 else 0.45) if "mid" in text else 0.85 if "useful" in text else 0.05
            answers[qid] = {"type": "choice", "probabilities": {"useful": p, "chatter": 1 - p}}
        data = json.dumps({"answers": answers, "usage": {"input_tokens": 7}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    calls: ClassVar[list[int]] = []
    busy: ClassVar[int] = 0

    def do_GET(self):
        path = self.path.split("?")[0]
        status, body = self.routes.get(path, (404, {"code": 404, "message": "not found"}))
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class TestClientLive(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def opts(self, **kwargs):
        defaults = {"base_url": self.base, "cache": False, "retry_empty": 0, "retries": 0}
        defaults.update(kwargs)
        return tc.Opts(**defaults)

    def setUp(self):
        _Handler.routes = {
            "/2/status/20": (200, {"code": 200, "status": {"type": "status", "id": "20", "text": "hi"}}),
            "/2/status/404": (200, {"code": 404, "message": "not found"}),
        }

    async def test_get_ok(self):
        opts = self.opts()
        reply = await tc.fetch_payload("status", "20", opts)
        self.assertEqual(reply["code"], 200)
        self.assertEqual(reply["__http_status__"], 200)

    async def test_body_code_404_is_api_error(self):
        opts = self.opts()
        reply = await tc.fetch_payload("status", "404", opts)
        self.assertEqual(tc.exit_code(reply), tc.EXIT_API_ERROR)

    async def test_cache_hit_skips_network(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"TWEETCIDER_CACHE_DIR": tmp}):
            opts = self.opts(cache=True, cache_ttl=60)
            first = await tc.fetch_payload("status", "20", opts)
            # A second call with the server routes removed must still succeed.
            _Handler.routes = {}
            second = await tc.fetch_payload("status", "20", opts)
            self.assertEqual(first["status"]["id"], second["status"]["id"])

    async def test_pagination_merges_pages(self):
        _Handler.routes = {
            "/2/search": (200, {"code": 200, "results": [], "cursor": {"bottom": "c2"}}),
        }
        pages = [
            {"code": 200, "results": [{"id": "1"}], "cursor": {"bottom": "c2"}},
            {"code": 200, "results": [{"id": "2"}], "cursor": {"bottom": "c3"}},
            {"code": 200, "results": [{"id": "3"}], "cursor": {"bottom": "c4"}},
        ]
        calls = []

        class SequencingHandler(_Handler):
            def do_GET(self):
                calls.append(self.path)
                index = min(len(calls) - 1, len(pages) - 1)
                data = json.dumps(pages[index]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), SequencingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            opts = self.opts(base_url=f"http://127.0.0.1:{server.server_address[1]}", pages=3)
            reply = await tc.fetch_payload("search", "hi", opts)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual([item["id"] for item in reply["results"]], ["1", "2", "3"])
        self.assertEqual(reply["cursor"]["bottom"], "c4")
        self.assertEqual(reply["__pages_fetched__"], 3)

    async def test_max_items_truncates(self):
        _Handler.routes = {
            "/2/search": (200, {"code": 200, "results": [{"id": "1"}, {"id": "2"}, {"id": "3"}], "cursor": {"bottom": "next"}}),
        }
        opts = self.opts(max_items=2)
        reply = await tc.fetch_payload("search", "hi", opts)
        self.assertEqual(len(reply["results"]), 2)
        self.assertIsNone(reply["cursor"]["bottom"])

    async def test_later_page_failure_is_not_success(self):
        client = mock.Mock()
        client.get = mock.AsyncMock(side_effect=[
            tc.Reply(200, {"code": 200, "results": [{"id": "1"}], "cursor": {"bottom": "next"}}, ""),
            tc.Reply(503, {"message": "unavailable"}, ""),
        ])
        with self.assertRaises(tc.TweetciderError) as ctx:
            await tc.paginate(client, "/2/search", {}, self.opts(pages=2))
        self.assertEqual(ctx.exception.code, tc.EXIT_API_ERROR)
        self.assertIn("page 2", str(ctx.exception))

    async def test_exact_page_limit_keeps_cursor(self):
        client = mock.Mock()
        client.get = mock.AsyncMock(return_value=tc.Reply(
            200, {"code": 200, "results": [{"id": "1"}], "cursor": {"bottom": "next"}}, ""
        ))
        result = await tc.paginate(client, "/2/search", {}, self.opts(max_items=1))
        self.assertEqual(result["cursor"]["bottom"], "next")

    async def test_download_api_error(self):
        with self.assertRaises(tc.TweetciderError) as ctx:
            await tc.download("404", self.opts())
        self.assertEqual(ctx.exception.code, tc.EXIT_API_ERROR)

    async def test_download_failed_media_returns_partial(self):
        _Handler.routes["/2/status/20"] = (200, {"code": 200, "status": {
            "type": "status", "id": "20", "text": "x",
            "media": {"photos": [{"url": self.base + "/missing.jpg"}]},
        }})
        with tempfile.TemporaryDirectory() as tmp:
            result = await tc.download("20", self.opts(), out_dir=pathlib.Path(tmp))
        self.assertEqual(result, tc.EXIT_PARTIAL)

    async def test_empty_retry_retries_once(self):
        calls = {"n": 0}

        class FlakyHandler(_Handler):
            def do_GET(self):
                calls["n"] += 1
                body = {"code": 200, "results": []} if calls["n"] == 1 else {"code": 200, "results": [{"id": "1"}]}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"TWEETCIDER_CACHE_DIR": tmp}):
                opts = self.opts(base_url=f"http://127.0.0.1:{server.server_address[1]}", retry_empty=1, cache=True)
                reply = await tc.fetch_payload("search", "hi", opts)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(reply["results"], [{"id": "1"}])

    async def test_network_error_raises(self):
        opts = self.opts(base_url="http://127.0.0.1:1", timeout=1.0)
        with self.assertRaises(tc.TweetciderError):
            await tc.fetch_payload("status", "20", opts)

    async def test_mine_keeps_focal_and_useful_only(self):
        post = lambda i, text, **kw: {"type": "status", "id": i, "text": text, **kw}
        _Handler.calls = []
        _Handler.routes = {
            "/2/conversation/1": (200, {"code": 200, "status": post("1", "focal"), "thread": [],
                                        "replies": [post("2", "useful fact"), post("3", "lol"), post("2", "useful fact"),
                                                    post("5", "great post"), post("6", "the same copy pasted reply"),
                                                    post("7", "@x the same copy pasted reply")]}),
            "/2/status/1/quotes": (200, {"code": 200, "results": [post("4", "useful quote", quote=post("1", "focal"))]}),
        }
        out = await tc.mine_payload("1", "q", self.opts(), key_file=None, threshold=0.5, limit=None,
                                    decider_url=self.base + "/decide")
        self.assertEqual(out["status"]["id"], "1")
        self.assertEqual(sorted(s["id"] for s in out["results"]), ["2", "4"])
        mined = out["mined"]
        self.assertEqual((mined["scored"], mined["relevant"], mined["shown"], mined["duplicates"]), (5, 2, 2, 1))
        self.assertEqual(_Handler.calls, [5])  # one batch over the 5 non-duplicates
        self.assertEqual(out["results"][0]["relevance"], 0.85)
        self.assertNotIn("quote", next(s for s in out["results"] if s["id"] == "4"))

    async def test_mine_rescores_borderline_posts_alone(self):
        post = lambda i, text: {"type": "status", "id": i, "text": text}
        _Handler.calls = []
        _Handler.routes = {
            "/2/conversation/1": (200, {"code": 200, "status": post("1", "focal"), "thread": [],
                                        "replies": [post("2", "useful"), post("3", "mid a"), post("4", "mid b")]}),
            "/2/status/1/quotes": (404, {"code": 404}),
        }
        out = await tc.mine_payload("1", "q", self.opts(), key_file=None, threshold=0.5, limit=None,
                                    decider_url=self.base + "/decide")
        # batched they score 0.45 (dropped); alone 0.55 (kept) - alone wins
        self.assertEqual(sorted(s["id"] for s in out["results"]), ["2", "3", "4"])
        self.assertEqual(out["mined"]["rescored_alone"], 2)
        self.assertEqual(_Handler.calls, [3, 1, 1])

    async def test_mine_retries_a_busy_decider(self):
        post = lambda i, text: {"type": "status", "id": i, "text": text}
        _Handler.calls, _Handler.busy = [], 1
        _Handler.routes = {
            "/2/conversation/1": (200, {"code": 200, "status": post("1", "f"), "replies": [post("2", "useful")]}),
            "/2/status/1/quotes": (404, {"code": 404}),
        }
        out = await tc.mine_payload("1", "q", self.opts(retries=1), key_file=None, threshold=0.5, limit=None,
                                    decider_url=self.base + "/decide")
        self.assertEqual([s["id"] for s in out["results"]], ["2"])
        self.assertEqual(_Handler.busy, 0)

    def test_clef_batches_respect_questions_and_budget(self):
        posts = [{"id": str(i), "text": "x"} for i in range(130)]
        self.assertEqual([len(b) for b in tc.clef_batches(posts, 100, 24000)], [64, 64, 2])
        big = [{"id": str(i), "text": "word " * 150} for i in range(40)]
        batches = tc.clef_batches(big, 100, 2000)
        self.assertEqual(sum(map(len, batches)), 40)
        for batch in batches:
            cost = 100 + sum(tc.count_tokens(tc._candidate(s)) + tc._CLEF_QUESTION_TOKENS for s in batch)
            self.assertLessEqual(cost, 2000)

    async def test_mine_quotes_failure_is_a_warning(self):
        _Handler.routes = {"/2/conversation/1": (200, {"code": 200, "status": {"type": "status", "id": "1", "text": "f"}, "replies": []})}
        out = await tc.mine_payload("1", "q", self.opts(), key_file=None, threshold=0.5, limit=None,
                                    decider_url=self.base + "/decide")
        self.assertEqual(out["results"], [])
        self.assertIn("quotes unavailable", out["mined"]["warning"])

    async def test_mine_without_credentials_is_usage_error(self):
        with mock.patch.dict(os.environ, clear=True), self.assertRaises(tc.TweetciderError) as ctx:
            await tc.mine_payload("1", "q", self.opts(), key_file=None, threshold=0.5, limit=None)
        self.assertEqual(ctx.exception.code, tc.EXIT_USAGE)

    async def test_unknown_command_is_usage_error(self):
        opts = self.opts()
        with self.assertRaises(tc.TweetciderError) as ctx:
            await tc.fetch_payload("bogus", "20", opts)
        self.assertEqual(ctx.exception.code, tc.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
