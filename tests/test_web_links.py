""""Add a link": safe fetching, extraction, the /add routes, storage, and the reading list feed.

No test touches the network: pages come from fixtures through a fake fetcher
or a mocked urllib3 manager, and the model is a stub.
"""
from __future__ import annotations

import http.client
import json
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import quote, urlencode
from xml.etree import ElementTree as ET

import urllib3
from test_web_ui import EMAIL, GOOD_TOKEN, HOST, ORIGIN, SECRET, SUMMARIZED_ID, _config, _message, _seed, _verify

from newsletter_summary import safe_fetch, web_article
from newsletter_summary.gmail_client import GMAIL_ID_PATTERN, NewsletterMessage
from newsletter_summary.image_proxy import Image, ImageCache, ImageProxy
from newsletter_summary.openrouter import SummaryItem, summarize_article
from newsletter_summary.platform_server import PlatformHTTPServer
from newsletter_summary.rss import all_feed_token, digest_feed_token, full_feed_token
from newsletter_summary.safe_fetch import BlockedAddress, Fetched, FetchError, TimedOut, TooLarge, UnsupportedType
from newsletter_summary.store import Store
from newsletter_summary.web_article import (
    decode_html,
    extract,
    fetch_page,
    first_url,
    is_web_id,
    normalized_url,
    web_id,
)
from newsletter_summary.web_ui import build_web_server, csrf_token

FIXTURES = Path(__file__).with_name("fixtures") / "web"
PUBLIC_IP = "93.184.216.34"
ARTICLE_URL = "https://www.riversideledger.example/news/2026/11/rail-yard-park"
CANONICAL = "https://www.riversideledger.example/news/2026/11/rail-yard-park?ref=home"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def public(_host: str, _port: int) -> list[str]:
    return [PUBLIC_IP]


def resolving(mapping: dict[str, list[str]]):
    def resolve(host: str, _port: int) -> list[str]:
        return mapping[host]

    return resolve


class _Response:
    def __init__(self, status: int = 200, headers: dict[str, str] | None = None, body: bytes = b"<html></html>",
                 error: Exception | None = None):
        self.status = status
        self.headers = headers if headers is not None else {"Content-Type": "text/html; charset=utf-8"}
        self.body = body
        self.error = error
        self.released = False

    def stream(self, size: int, decode_content: bool = True):
        if self.error is not None:
            raise self.error
        for index in range(0, len(self.body), size):
            yield self.body[index : index + size]

    def release_conn(self) -> None:
        self.released = True


def manager(*responses: _Response | Exception) -> Mock:
    mock = Mock()
    mock.request.side_effect = list(responses)
    return mock


class SafeFetchTests(unittest.TestCase):
    """The guarded fetcher shared by the image proxy and "Add a link"."""

    def test_private_and_special_addresses_are_never_requested(self) -> None:
        for addresses in (["10.0.0.5"], ["127.0.0.1"], ["169.254.169.254"], [PUBLIC_IP, "192.168.1.2"], ["::1"]):
            with self.subTest(addresses=addresses):
                mock = manager(_Response())
                with self.assertRaises(BlockedAddress) as caught:
                    fetch_page("https://internal.example/", resolver=resolving({"internal.example": addresses}),
                               manager=mock)
                self.assertEqual(caught.exception.reason, "blocked")
                mock.request.assert_not_called()

    def test_urls_that_are_not_public_http_are_refused(self) -> None:
        for url in ("file:///etc/passwd", "ftp://example.com/", "https://example.com:8443/", "https://u:p@example.com/",
                    "javascript:alert(1)"):
            with self.subTest(url=url), self.assertRaises(FetchError) as caught:
                fetch_page(url, resolver=public, manager=manager(_Response()))
            self.assertEqual(caught.exception.reason, "invalid")

    def test_redirects_to_private_addresses_are_blocked_and_redirects_are_capped(self) -> None:
        resolver = resolving({"news.example": [PUBLIC_IP], "admin.internal": ["10.1.2.3"]})
        mock = manager(_Response(302, {"Location": "http://admin.internal/"}))
        with self.assertRaises(BlockedAddress):
            fetch_page("https://news.example/story", resolver=resolver, manager=mock)
        self.assertEqual(mock.request.call_count, 1)
        # An IP literal resolves to itself, as getaddrinfo does.
        mock = manager(_Response(301, {"Location": "http://169.254.169.254/latest/meta-data/"}))
        with self.assertRaises(BlockedAddress):
            fetch_page("https://news.example/story", manager=mock,
                       resolver=resolving({"news.example": [PUBLIC_IP], "169.254.169.254": ["169.254.169.254"]}))
        self.assertEqual(mock.request.call_count, 1)
        loop = [_Response(302, {"Location": f"/hop{index}"}) for index in range(safe_fetch.MAX_REDIRECTS + 1)]
        with self.assertRaisesRegex(FetchError, "too many redirects"):
            fetch_page("https://news.example/story", resolver=public, manager=manager(*loop))
        # A good redirect is followed and the final URL is reported.
        mock = manager(_Response(301, {"Location": "/final"}), _Response(body=b"<p>ok</p>"))
        fetched = fetch_page("https://news.example/story", resolver=public, manager=mock)
        self.assertEqual(fetched.url, "https://news.example/final")
        self.assertEqual(fetched.data, b"<p>ok</p>")

    def test_only_html_is_accepted(self) -> None:
        for content_type in ("application/pdf", "image/png", "application/json", "text/plain", ""):
            with self.subTest(content_type=content_type):
                response = _Response(headers={"Content-Type": content_type})
                with self.assertRaises(UnsupportedType):
                    fetch_page("https://news.example/", resolver=public, manager=manager(response))
                self.assertTrue(response.released)
        for content_type in ("text/html", "TEXT/HTML; charset=ISO-8859-1", "application/xhtml+xml"):
            with self.subTest(content_type=content_type):
                fetched = fetch_page("https://news.example/", resolver=public,
                                     manager=manager(_Response(headers={"Content-Type": content_type})))
                self.assertIn(fetched.content_type, web_article.HTML_TYPES)
        fetched = fetch_page("https://news.example/", resolver=public, manager=manager(
            _Response(headers={"Content-Type": 'text/html; charset="Windows-1252"'})))
        self.assertEqual(fetched.charset, "windows-1252")

    def test_size_status_and_time_limits(self) -> None:
        big = web_article.MAX_PAGE_BYTES + 1
        with self.assertRaises(TooLarge):
            fetch_page("https://news.example/", resolver=public, manager=manager(
                _Response(headers={"Content-Type": "text/html", "Content-Length": str(big)})))
        with self.assertRaises(TooLarge):
            fetch_page("https://news.example/", resolver=public, manager=manager(_Response(body=b"x" * big)))
        with self.assertRaises(safe_fetch.HTTPStatusError) as caught:
            fetch_page("https://news.example/", resolver=public, manager=manager(_Response(404)))
        self.assertEqual(str(caught.exception), "HTTP 404")
        timeout = urllib3.exceptions.ReadTimeoutError(None, "/", "read timed out")  # ty: ignore[invalid-argument-type]
        with self.assertRaises(TimedOut):
            fetch_page("https://news.example/", resolver=public, manager=manager(timeout))
        with self.assertRaises(TimedOut):
            fetch_page("https://news.example/", resolver=public, manager=manager(_Response(error=timeout)))
        ticks = iter([0.0, 0.0, 100.0, 100.0])
        with self.assertRaises(TimedOut):
            fetch_page("https://news.example/", resolver=public, manager=manager(_Response(body=b"x" * 10)),
                       clock=lambda: next(ticks))

    def test_sends_a_browser_like_user_agent_naming_the_app(self) -> None:
        mock = manager(_Response())
        fetch_page("https://news.example/", resolver=public, manager=mock)
        headers = mock.request.call_args.kwargs["headers"]
        self.assertTrue(headers["User-Agent"].startswith("Mozilla/5.0"))
        self.assertIn("newsletter-summary", headers["User-Agent"])
        self.assertFalse(mock.request.call_args.kwargs["redirect"])

    def test_connected_peer_is_rechecked(self) -> None:
        """DNS said public, but the socket connects to loopback: refused before any byte is sent."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        hits = []

        class Local(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Local)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = int(server.server_address[1])
            with self.assertRaises(BlockedAddress):
                fetch_page(f"http://127.0.0.1:{port}/", resolver=public, allowed_ports=frozenset({port}))
            self.assertEqual(hits, [])
        finally:
            server.shutdown()
            server.server_close()


    def test_gzip_responses_are_decompressed(self) -> None:
        """Real servers gzip HTML; the fetcher must hand back decoded bytes (urllib3 does not by default)."""
        import gzip
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from newsletter_summary import safe_fetch

        page = b"<html><head><title>Zipped</title></head><body><p>Hello</p></body></html>"

        class Gzipped(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = gzip.compress(page)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Gzipped)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = int(server.server_address[1])
            with patch.object(safe_fetch, "is_public_address", return_value=True):
                fetched = fetch_page(f"http://127.0.0.1:{port}/", resolver=public, allowed_ports=frozenset({port}))
            self.assertEqual(fetched.data, page)
        finally:
            server.shutdown()
            server.server_close()


class ExtractionTests(unittest.TestCase):
    def test_realistic_article_metadata_and_clean_content(self) -> None:
        article = extract(decode_html(fixture("article.html"), "utf-8"), ARTICLE_URL + "?utm_source=newsletter")
        # rel=canonical wins, without its tracking parameter.
        self.assertEqual(article.canonical_url, CANONICAL)
        self.assertEqual(article.title, "Council approves rail yard park after decade of debate")
        self.assertEqual(article.site_name, "The Riverside Ledger")
        self.assertEqual(article.byline, "Sam Rivera")
        self.assertEqual(article.published, "2026-11-20T14:30:00+00:00")
        self.assertEqual(article.lead_image, "https://www.riversideledger.example/images/rail-yard-hero.jpg")
        self.assertFalse(article.thin)
        self.assertGreater(article.words, 200)
        html = article.content_html
        self.assertIn("The city council voted seven to two", html)
        self.assertIn("State regulators signed off", html)
        # Site chrome, scripts, and styles are gone.
        for chrome in ("Most read", "We use cookies", "Sports", "window.analytics", "<script", "<style", "Privacy"):
            self.assertNotIn(chrome, html, chrome)
        # Images are absolute; the lazy one is recovered; tracking parameters are dropped from links.
        self.assertIn('src="https://www.riversideledger.example/images/rail-yard-hero.jpg"', html)
        self.assertEqual(html.count("rail-yard-hero.jpg"), 1)
        self.assertIn('src="https://cdn.riversideledger.example/maps/park-plan.png"', html)
        self.assertIn('href="https://www.riversideledger.example/news/parks"', html)
        self.assertNotIn("data:image", html)

    def test_thin_paywalled_page_is_marked_and_keeps_what_is_visible(self) -> None:
        article = extract(decode_html(fixture("paywall.html")), "https://marketweekly.example/shipping")
        self.assertTrue(article.thin)
        self.assertLess(article.words, web_article.THIN_WORDS)
        self.assertEqual(article.title, "The hidden cost of cheap shipping")
        self.assertEqual(article.site_name, "Market Weekly")
        self.assertIn("Retailers have spent a decade", article.content_html)
        self.assertNotIn("<script", article.content_html)

    def test_charset_from_meta_or_header_is_decoded(self) -> None:
        data = fixture("latin1.html")
        # No charset in the header: the <meta http-equiv> declaration is used.
        article = extract(decode_html(data), "https://lejournal.example/cafe")
        self.assertIn("Café culture in Montréal", article.content_html)
        self.assertIn("crème brûlée costs €3 — “quoted”", article.content_html)
        self.assertEqual(article.site_name, "Le Journal")
        # The header's charset is used when given.
        self.assertIn("naïve", decode_html("<p>naïve</p>".encode("iso-8859-1"), "iso-8859-1"))
        # UTF-8 with no declaration at all.
        self.assertIn("naïve", decode_html("<html><body><p>naïve café</p></body></html>".encode()))

    def test_page_without_metadata_falls_back_to_the_host_and_final_url(self) -> None:
        article = extract("<html><body><p>Short.</p></body></html>", "https://www.blog.example/post#comments")
        self.assertEqual(article.site_name, "blog.example")
        self.assertEqual(article.canonical_url, "https://www.blog.example/post")
        self.assertEqual(article.title, "blog.example")
        self.assertTrue(article.thin)
        self.assertEqual(article.published, "")

    def test_text_containing_a_url(self) -> None:
        cases = {
            "https://example.com/a": "https://example.com/a",
            "Great read https://example.com/story?id=3. via @someone": "https://example.com/story?id=3",
            "Title — https://en.wikipedia.org/wiki/Python_(language)": "https://en.wikipedia.org/wiki/Python_(language)",
            "(see https://example.com/x)": "https://example.com/x",
            "  http://example.com  ": "http://example.com",
            "no link here": "",
            "javascript:alert(1)": "",
            "ftp://example.com/file": "",
            "https://example.com:8080/x": "",
            "": "",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(first_url(text), expected)

    def test_web_ids_never_look_like_gmail_ids(self) -> None:
        item_id = web_id(CANONICAL)
        self.assertRegex(item_id, r"^web-[0-9a-f]{20}$")
        self.assertTrue(is_web_id(item_id))
        self.assertIsNone(GMAIL_ID_PATTERN.match(item_id))
        self.assertFalse(is_web_id(SUMMARIZED_ID))
        self.assertEqual(web_id(CANONICAL), item_id)
        self.assertNotEqual(web_id(CANONICAL + "x"), item_id)
        self.assertEqual(normalized_url("https://e.example/a?utm_source=x&id=2#frag"), "https://e.example/a?id=2")


class SummarizeArticleTests(unittest.TestCase):
    def test_uses_the_strict_schema_with_an_article_prompt_and_keeps_the_canonical_url(self) -> None:
        reply = {"choices": [{"message": {"content": json.dumps({
            "title": "Model title", "publication": "Ledger", "authors": ["Sam Rivera"],
            "url": "https://evil.example/elsewhere", "content_access": "full",
            "bullets": ["- The council approved a $48 million park.", "Phase one opens in 2028.", "c", "d"],
        })}}]}
        response = Mock(status_code=200)
        response.json.return_value = reply
        with patch.dict("os.environ", {"TEST_KEY": "k"}), patch(
            "newsletter_summary.openrouter.requests.post", return_value=response
        ) as post:
            item = summarize_article(
                api_key_env="TEST_KEY", keychain_service=None, keychain_account=None, model="anthropic/claude-opus-5.5",
                max_input_chars=1000, app_name="App", site_url="", base_prompt="Be useful.", title="Council approves",
                url=CANONICAL, site_name="The Riverside Ledger", byline="Sam Rivera", body_markdown="Body text",
                links=[],
            )
        assert item is not None
        self.assertEqual(item.url, CANONICAL)
        self.assertEqual(item.bullets, ["The council approved a $48 million park.", "Phase one opens in 2028.", "c"])
        payload = post.call_args.kwargs["json"]
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        system, user = (message["content"] for message in payload["messages"])
        self.assertIn("web article", system)
        self.assertIn("The bullets replace reading the article", system)
        self.assertNotIn("email", system)
        self.assertIn(f"SOURCE_URL: {CANONICAL}", user)
        self.assertIn("Body text", user)


def _article_page(name: str = "article.html", url: str = ARTICLE_URL, charset: str = "utf-8") -> Fetched:
    return Fetched(url=url, content_type="text/html", charset=charset, data=fixture(name))


class AddLinkWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _config(Path(self.tmp.name))
        _seed(self.config)
        self.gmail_fetches: list[str] = []
        self.page_fetches: list[str] = []
        self.pages: dict[str, Fetched | Exception] = {}
        self.summaries: list[str] = []
        self.summary_result: SummaryItem | Exception | None = SummaryItem(
            title="Model title", url=CANONICAL, publication="Ledger", authors=["Model Author"],
            bullets=["The council approved the park 7-2.", "Phase one costs $48 million and opens in 2028."],
            content_access="full",
        )
        self.refreshes = 0

        def gmail(gmail_id: str) -> NewsletterMessage | None:
            self.gmail_fetches.append(gmail_id)
            raise AssertionError("Gmail must not be called")

        def fetch_page(url: str) -> Fetched:
            self.page_fetches.append(url)
            page = self.pages.get(url, FetchError("unreachable"))
            if isinstance(page, Exception):
                raise page
            return page

        def summarize(article) -> SummaryItem | None:
            self.summaries.append(article.canonical_url)
            if isinstance(self.summary_result, Exception):
                raise self.summary_result
            return self.summary_result

        def refresh() -> None:
            self.refreshes += 1

        self.server = build_web_server(
            self.config, address=("127.0.0.1", 0), verify=_verify, fetch=gmail, secret=SECRET,
            refresh_reader=refresh, fetch_page=fetch_page, summarize_web=summarize, background=lambda job: job(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method: str, path: str, *, body: dict[str, str] | None = None,
                origin: str | None = ORIGIN) -> tuple[int, dict[str, str], str]:
        headers = {"Host": HOST, "Content-Type": "application/x-www-form-urlencoded",
                   "Cf-Access-Jwt-Assertion": GOOD_TOKEN}
        if origin is not None:
            headers["Origin"] = origin
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=urlencode(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        payload = response.read().decode("utf-8", errors="replace")
        conn.close()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, payload

    def add(self, url: str, origin: str | None = ORIGIN, csrf: str | None = None) -> tuple[int, dict[str, str], str]:
        token = csrf_token(SECRET, EMAIL, "add-link", "") if csrf is None else csrf
        return self.request("POST", "/add", body={"url": url, "csrf": token}, origin=origin)

    def items(self) -> dict[str, Any]:
        with Store(self.config.storage.sqlite_path) as store:
            return {item.gmail_id: item for item in store.list_full_items(100)}

    def test_get_add_has_no_side_effects_and_serves_a_self_submitting_form(self) -> None:
        status, headers, body = self.request("GET", "/add?" + urlencode({"url": ARTICLE_URL}))
        self.assertEqual(status, 200)
        self.assertEqual(self.page_fetches, [])
        self.assertEqual(self.items(), {})
        self.assertEqual(headers["cache-control"], "no-store, no-transform")
        form = re.search(r'<form id="add-form" method="post" action="/add">(.*?)</form>', body, re.S)
        assert form is not None
        self.assertIn(f'name="url" value="{ARTICLE_URL}"', form.group(1))
        self.assertIn(f'name="csrf" value="{csrf_token(SECRET, EMAIL, "add-link", "")}"', form.group(1))
        nonce = re.search(r"'nonce-([^']+)'", headers["content-security-policy"])
        assert nonce is not None
        scripts = re.findall(r'<script nonce="([^"]+)">(.*?)</script>', body, re.S)
        self.assertEqual([found for found, _ in scripts], [nonce.group(1)])
        self.assertIn('getElementById("add-form")', scripts[0][1])
        self.assertIn("visibilityState", scripts[0][1])

    def test_get_add_extracts_the_url_from_shared_text(self) -> None:
        shared = f"Council approves rail yard park {ARTICLE_URL}?utm_source=share"
        status, _, body = self.request("GET", "/add?url=" + quote(shared))
        self.assertEqual(status, 200)
        self.assertIn(f'name="url" value="{ARTICLE_URL}?utm_source=share"', body)
        status, _, body = self.request("GET", "/add?text=" + quote(f"Look: {ARTICLE_URL}"))
        self.assertEqual(status, 200)
        self.assertIn(f'name="url" value="{ARTICLE_URL}"', body)
        status, _, body = self.request("GET", "/add?url=" + quote("no link at all"))
        self.assertEqual(status, 400)
        self.assertIn("does not contain a web link", body)
        self.assertEqual(self.page_fetches, [])

    def test_post_add_requires_origin_and_csrf(self) -> None:
        self.pages[ARTICLE_URL] = _article_page()
        cases = [
            (None, None), ("null", None), ("https://evil.example", None), (ORIGIN, ""), (ORIGIN, "0" * 64),
            (ORIGIN, csrf_token(SECRET, EMAIL, "add", "")),
            (ORIGIN, csrf_token(SECRET, "other@example.com", "add-link", "")),
        ]
        for origin, csrf in cases:
            with self.subTest(origin=origin, csrf=csrf):
                status, _, _ = self.add(ARTICLE_URL, origin=origin, csrf=csrf)
                self.assertEqual(status, 403)
        self.assertEqual(self.page_fetches, [])
        self.assertEqual(self.items(), {})

    def test_add_stores_the_page_summarizes_it_and_shows_it(self) -> None:
        self.pages[ARTICLE_URL] = _article_page()
        status, headers, _ = self.add(ARTICLE_URL)
        item_id = web_id(CANONICAL)
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], f"/item/{item_id}?added=1")
        item = self.items()[item_id]
        self.assertEqual(item.source_kind, "web")
        self.assertEqual(item.url, CANONICAL)
        self.assertEqual(item.source_url, ARTICLE_URL)
        self.assertEqual(item.publication, "The Riverside Ledger")
        self.assertEqual(item.authors, ["Sam Rivera"])
        self.assertEqual(item.bullets, ["The council approved the park 7-2.", "Phase one costs $48 million and opens in 2028."])
        self.assertEqual(item.content_access, "full")
        self.assertEqual(item.source_gmail_url, "")
        # The body lives in the content store, raw and sanitized, for re-sanitizing later.
        self.assertEqual(item.content_html, "")
        with Store(self.config.storage.sqlite_path) as store:
            content = store.get_content(item_id)
        assert content is not None
        self.assertIn("rail yard", content.raw_html)
        self.assertIn("The city council voted", content.content_html)
        self.assertEqual(self.summaries, [CANONICAL])
        self.assertEqual(self.refreshes, 1)
        self.assertEqual(self.gmail_fetches, [])

        status, _, page = self.request("GET", f"/item/{item_id}?added=1")
        self.assertEqual(status, 200)
        self.assertIn('<span class="status">Web</span> The Riverside Ledger', page)
        self.assertIn("Council approves rail yard park after decade of debate", page)
        self.assertIn("Sam Rivera · Nov 20, 2026", page)
        self.assertIn(f'href="{CANONICAL}"', page)
        self.assertIn(f'action="/item/{item_id}/remove"', page)
        self.assertIn("The council approved the park 7-2.", page)
        self.assertIn("https://rss.example.com/img/", page)
        self.assertNotIn('src="https://www.riversideledger.example/images/', page)
        # The page's own headline is not repeated under the title.
        self.assertEqual(page.count("Council approves rail yard park after decade of debate</"), 1)

        _, _, full = self.request("GET", "/full")
        self.assertIn('<span class="status">Web</span> <strong>The Riverside Ledger</strong>', full)
        self.assertIn(f'<a href="/item/{item_id}">', full)
        self.assertEqual(self.gmail_fetches, [])

    def test_canonical_url_dedupes_and_the_submitted_url_is_found_without_fetching(self) -> None:
        other = "https://riversideledger.example/amp/rail-yard-park"
        self.pages[ARTICLE_URL] = _article_page()
        self.pages[other] = _article_page(url=other)
        self.add(ARTICLE_URL)
        self.assertEqual(len(self.page_fetches), 1)
        # Same URL again: found by its submitted URL, never fetched.
        status, headers, _ = self.add(ARTICLE_URL + "#comments")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], f"/item/{web_id(CANONICAL)}?added=1")
        self.assertEqual(len(self.page_fetches), 1)
        # A different URL whose page has the same canonical URL: fetched, then deduped.
        status, headers, _ = self.add(other)
        self.assertEqual(headers["location"], f"/item/{web_id(CANONICAL)}?added=1")
        self.assertEqual(len(self.page_fetches), 2)
        self.assertEqual(list(self.items()), [web_id(CANONICAL)])
        self.assertEqual(len(self.summaries), 1)
        # GET /add for a saved page names it.
        _, _, body = self.request("GET", "/add?" + urlencode({"url": ARTICLE_URL}))
        self.assertIn("<h2>Council approves rail yard park after decade of debate</h2>", body)
        self.assertIn("Already on", body)

    def test_thin_page_is_saved_as_partial_with_a_note(self) -> None:
        url = "https://marketweekly.example/shipping"
        self.pages[url] = _article_page("paywall.html", url=url)
        self.summary_result = None
        self.add(url)
        item_id = web_id(url)
        item = self.items()[item_id]
        self.assertEqual(item.content_access, "partial")
        self.assertEqual(item.bullets, [])
        _, _, page = self.request("GET", f"/item/{item_id}")
        self.assertIn("Couldn&#x27;t extract the full article, perhaps because of a paywall or a JavaScript-only page", page)
        self.assertIn("Retailers have spent a decade", page)

    def test_model_failure_still_saves_the_page_without_bullets(self) -> None:
        self.pages[ARTICLE_URL] = _article_page()
        self.summary_result = RuntimeError("OpenRouter 500")
        with self.assertLogs(level="ERROR") as logs:
            status, _, _ = self.add(ARTICLE_URL)
        self.assertEqual(status, 303)
        self.assertIn("Could not summarize web page", "\n".join(logs.output))
        item = self.items()[web_id(CANONICAL)]
        self.assertEqual(item.bullets, [])
        self.assertEqual(self.refreshes, 1)

    def test_fetch_failures_show_a_friendly_page_and_store_nothing(self) -> None:
        cases = {
            BlockedAddress("host resolves to a non-public address"): "private or local network",
            UnsupportedType("not an allowed content type"): "not a web page",
            TooLarge("response is too large"): "larger than 5 MB",
            TimedOut("timed out"): "took too long",
            safe_fetch.HTTPStatusError("HTTP 403"): "answered with an error (HTTP 403)",
            FetchError("cannot resolve host: gaierror"): "could not be reached",
        }
        for error, message in cases.items():
            with self.subTest(error=type(error).__name__):
                self.pages[ARTICLE_URL] = error
                status, headers, body = self.add(ARTICLE_URL)
                self.assertEqual(status, 502)
                self.assertEqual(headers["cache-control"], "no-store, no-transform")
                self.assertIn(message, body)
                self.assertIn(f'href="{ARTICLE_URL}"', body)
                self.assertIn("Nothing was saved", body)
        self.assertEqual(self.items(), {})
        self.assertEqual(self.summaries, [])
        self.assertEqual(self.refreshes, 0)
        with Store(self.config.storage.sqlite_path) as store:
            self.assertEqual(store.content_ids(), [])

    def test_web_ids_never_reach_gmail(self) -> None:
        unknown = web_id("https://never-saved.example/")
        for method, path in (("GET", f"/item/{unknown}"), ("GET", f"/read/{unknown}"), ("POST", f"/read/{unknown}"),
                             ("GET", "/item/web-NOTHEX"), ("GET", "/item/web-")):
            with self.subTest(path=path):
                body = {"csrf": csrf_token(SECRET, EMAIL, "add", unknown)} if method == "POST" else None
                status, _, _ = self.request(method, path, body=body)
                self.assertEqual(status, 404)
        self.pages[ARTICLE_URL] = _article_page()
        self.add(ARTICLE_URL)
        item_id = web_id(CANONICAL)
        # A saved web item is readable, but /read (the Gmail path) still refuses it.
        self.assertEqual(self.request("GET", f"/item/{item_id}")[0], 200)
        self.assertEqual(self.request("GET", f"/read/{item_id}")[0], 404)
        self.assertEqual(self.gmail_fetches, [])

    def test_remove_deletes_the_item_and_its_content(self) -> None:
        self.pages[ARTICLE_URL] = _article_page()
        self.add(ARTICLE_URL)
        item_id = web_id(CANONICAL)
        status, _, _ = self.request("POST", f"/item/{item_id}/remove", body={"csrf": "bad"})
        self.assertEqual(status, 403)
        status, headers, _ = self.request(
            "POST", f"/item/{item_id}/remove", body={"csrf": csrf_token(SECRET, EMAIL, "remove", item_id)}
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/full?removed=1")
        self.assertEqual(self.items(), {})
        with Store(self.config.storage.sqlite_path) as store:
            self.assertIsNone(store.get_content(item_id))
        self.assertEqual(self.refreshes, 2)
        self.assertEqual(self.request("GET", f"/item/{item_id}")[0], 404)

    def test_full_page_offers_the_box_bookmarklet_and_shortcut(self) -> None:
        _, _, full = self.request("GET", "/full")
        self.assertIn('<form class="filters" method="post" action="/add">', full)
        self.assertIn(f'value="{csrf_token(SECRET, EMAIL, "add-link", "")}"', full)
        bookmarklet = f"javascript:location.href=&#x27;{ORIGIN}/add?url=&#x27;+encodeURIComponent(location.href)"
        self.assertIn(f'href="{bookmarklet}"', full)
        self.assertIn(f'value="{ORIGIN}/add?url="', full)
        self.assertIn("Open URLs", full)
        self.assertIn("Share Sheet", full)
        self.assertNotIn("not installed yet", full)


class FeedTests(unittest.TestCase):
    """Web items in the Reading List feed, and nowhere else."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _config(Path(self.tmp.name))
        _seed(self.config)
        server = build_web_server(
            self.config, address=("127.0.0.1", 0), verify=_verify, fetch=lambda _: None, secret=SECRET,
            refresh_reader=lambda: None, fetch_page=lambda url: _article_page(url=url),
            summarize_web=lambda article: SummaryItem(
                title="t", url=CANONICAL, publication="p", authors=[], bullets=["Key point one."],
                content_access="full"),
            background=lambda job: job(),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", int(server.server_address[1]), timeout=5)
        conn.request("POST", "/add", body=urlencode({"url": ARTICLE_URL, "csrf": csrf_token(SECRET, EMAIL, "add-link", "")}),
                     headers={"Host": HOST, "Origin": ORIGIN, "Cf-Access-Jwt-Assertion": GOOD_TOKEN,
                              "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(conn.getresponse().status, 303)
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

        cache = ImageCache(Path(self.tmp.name) / "images", 10_000)
        self.public = PlatformHTTPServer(
            ("127.0.0.1", 0), self.config,
            images=ImageProxy(cache, max_bytes=1000, fetch=lambda url: Image(b"", "image/png")),
        )
        self.public_thread = threading.Thread(target=self.public.serve_forever, daemon=True)
        self.public_thread.start()
        self.env = patch.dict("os.environ", {"WEB_SECRET": SECRET})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.public.shutdown()
        self.public.server_close()
        self.public_thread.join(timeout=2)
        self.tmp.cleanup()

    def get(self, path: str) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", int(self.public.server_address[1]), timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def test_reading_list_feed_carries_the_web_item(self) -> None:
        status, payload = self.get(f"/full/{full_feed_token(SECRET)}/feed.xml")
        self.assertEqual(status, 200)
        items = ET.fromstring(payload).findall("./channel/item")
        web = [item for item in items if (item.findtext("guid") or "").startswith("newsletter-web:")]
        self.assertEqual(len(web), 1)
        item = web[0]
        self.assertEqual(item.findtext("guid"), "newsletter-web:" + web_id(CANONICAL).removeprefix("web-"))
        self.assertEqual(item.findtext("link"), CANONICAL)
        self.assertEqual(item.findtext("title"), "Council approves rail yard park after decade of debate")
        self.assertEqual(item.findtext(f"{DC_NS}creator"), "Sam Rivera")
        self.assertEqual(item.findtext("description"), "<ul><li>Key point one.</li></ul>")
        content = item.findtext(f"{CONTENT_NS}encoded") or ""
        self.assertIn("The city council voted seven to two", content)
        self.assertIn("https://rss.example.com/img/", content)
        self.assertNotIn('src="https://www.riversideledger.example/', content)
        self.assertIn(f'href="{CANONICAL}"', content)
        self.assertNotIn("mail.google.com", content)
        self.assertNotIn("<script", content)

    def test_web_items_stay_out_of_the_digest_and_everything_feeds(self) -> None:
        for kind, token in (("digest", digest_feed_token(SECRET)), ("all", all_feed_token(SECRET))):
            with self.subTest(kind=kind):
                status, payload = self.get(f"/{kind}/{token}/feed.xml")
                self.assertEqual(status, 200)
                self.assertNotIn(b"rail yard", payload.lower())
                self.assertNotIn(b"newsletter-web:", payload)


class BeforeMigrationTests(unittest.TestCase):
    """PostgreSQL without deploy/sql/2026-11-25-web-links.sql, simulated with SQLite."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _config(Path(self.tmp.name))
        _seed(self.config)
        path = self.config.storage.sqlite_path
        with sqlite3.connect(path) as connection:
            connection.execute("drop index if exists full_items_source_url")
            connection.execute("alter table full_items drop column source_url")
            connection.execute("alter table full_items drop column source_kind")
        connection.close()
        self.page_fetches: list[str] = []

        def fetch_page(url: str) -> Fetched:
            self.page_fetches.append(url)
            return _article_page(url=url)

        self.server = build_web_server(
            self.config, address=("127.0.0.1", 0), verify=_verify, fetch=_message, secret=SECRET,
            refresh_reader=lambda: None, fetch_page=fetch_page, summarize_web=lambda article: None,
            background=lambda job: job(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict[str, str] | None = None) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", int(self.server.server_address[1]), timeout=5)
        conn.request(method, path, body=urlencode(body) if body is not None else None,
                     headers={"Host": HOST, "Origin": ORIGIN, "Cf-Access-Jwt-Assertion": GOOD_TOKEN,
                              "Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        result = response.status, response.read().decode("utf-8")
        conn.close()
        return result

    def test_adding_a_link_is_refused_clearly_and_everything_else_works(self) -> None:
        status, body = self.request("POST", "/add", {"url": ARTICLE_URL, "csrf": csrf_token(SECRET, EMAIL, "add-link", "")})
        self.assertEqual(status, 503)
        self.assertIn("2026-11-25-web-links.sql", body)
        self.assertIn("Nothing was saved", body)
        self.assertEqual(self.page_fetches, [])
        # GET /add still renders (no lookup by submitted URL is possible yet).
        self.assertEqual(self.request("GET", "/add?" + urlencode({"url": ARTICLE_URL}))[0], 200)
        # The reading list page works and says what is missing.
        status, full = self.request("GET", "/full")
        self.assertEqual(status, 200)
        self.assertIn("Adding web links is not installed yet", full)
        # Email items can still be added and listed.
        status, _ = self.request("POST", f"/read/{SUMMARIZED_ID}", {"csrf": csrf_token(SECRET, EMAIL, "add", SUMMARIZED_ID)})
        self.assertEqual(status, 303)
        _, full = self.request("GET", "/full")
        self.assertIn(f'<a href="/item/{SUMMARIZED_ID}">Big News</a>', full)
        self.assertNotIn('<span class="status">Web</span>', full)
        _, status_page = self.request("GET", "/status")
        self.assertIn("Web links on the reading list", status_page)
        self.assertIn("missing: apply deploy/sql/2026-11-25-web-links.sql", status_page)
        with Store(self.config.storage.sqlite_path) as store:
            self.assertFalse(store.web_items_available())
            self.assertEqual([item.source_kind for item in store.list_full_items(10)], ["gmail"])


class ResanitizeTests(unittest.TestCase):
    def test_stored_web_content_is_rederived_like_email(self) -> None:
        from newsletter_summary.email_content import resanitize_all

        with tempfile.TemporaryDirectory() as directory:
            article = extract(decode_html(fixture("article.html")), ARTICLE_URL)
            from newsletter_summary.store import StoredContent

            with Store(Path(directory) / "db.sqlite3") as store:
                store.save_content(StoredContent(
                    gmail_id=article.web_id, subject=article.title, sender=article.site_name, received_at="",
                    raw_html=article.raw_html, raw_text="", content_html="<p>old</p>", sanitizer_version=0,
                    word_count=1,
                ))
                self.assertEqual(resanitize_all(store), (1, 0))
                content = store.get_content(article.web_id)
        assert content is not None
        self.assertEqual(content.content_html, article.content_html)


if __name__ == "__main__":
    unittest.main()
