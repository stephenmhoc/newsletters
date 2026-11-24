from __future__ import annotations

import dataclasses
import http.client
import os
import re
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from newsletter_summary.access_auth import AccessDenied
from newsletter_summary.config import (
    AppConfig,
    GmailConfig,
    RssConfig,
    ServerConfig,
    StorageConfig,
    SummaryConfig,
    WebConfig,
)
from newsletter_summary.gmail_client import NewsletterMessage
from newsletter_summary.image_proxy import Image, ImageCache, ImageFetchError, ImageProxy, proxied_image_url
from newsletter_summary.openrouter import SummaryItem
from newsletter_summary.platform_server import PlatformHTTPServer, mask_feed_tokens, mask_full_feed_token
from newsletter_summary.rss import all_feed_token, digest_feed_token
from newsletter_summary.store import Store
from newsletter_summary.web_ui import (
    WebHTTPServer,
    build_web_server,
    csrf_token,
    full_feed_token,
    start_web_listener,
)

SECRET = "test-web-secret-0123456789abcdef"
HOST = "newsletters.example.com"
ORIGIN = f"https://{HOST}"
EMAIL = "reader@example.com"
GOOD_TOKEN = "good-access-token"
SUMMARIZED_ID = "18c0ffee00000001"
SKIPPED_ID = "18c0ffee00000002"
UNKNOWN_ID = "18c0ffee000000ff"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"

EMAIL_HTML = """<html><head><style>p{}</style></head><body>
<div style="display:none">preview text</div>
<h1>Big News</h1>
<p>Hello <strong>reader</strong>. <a href="https://example.com/r?url=https%3A%2F%2Fexample.com%2Fstory">Story</a></p>
<img src="https://cdn.example.com/hero.jpg" width="600" alt="Hero">
<img src="https://cdn.example.com/open.gif" width="1" height="1">
<script>alert(1)</script>
<p><a href="https://example.com/unsubscribe">Unsubscribe</a></p>
</body></html>"""


def _config(root: Path, **web_overrides: Any) -> AppConfig:
    web = WebConfig(
        enabled=True,
        port=0,
        public_url=ORIGIN,
        access_team_domain="team-example.cloudflareaccess.com",
        access_aud="aud-123",
        allowed_emails=(EMAIL,),
    )
    return AppConfig(
        path=root / "config.yaml",
        root=root,
        gmail=GmailConfig(label="Newsletters/Skipped", user_id="me", timezone="America/New_York"),
        summary=SummaryConfig(
            openrouter_api_key_env="OPENROUTER_API_KEY",
            openrouter_keychain_service=None,
            openrouter_keychain_account=None,
            model="test-model",
            max_input_chars=1000,
            site_url="",
            app_name="Newsletter Summary",
            prompt="",
        ),
        rss=RssConfig(
            title="Newsletter Highlights",
            description="Daily highlights.",
            public_url="https://rss.example.com/feed.xml",
            max_items=None,
        ),
        server=ServerConfig(host="127.0.0.1", port=0),
        storage=StorageConfig(sqlite_path=root / "items.sqlite3", log_path=root / "log.txt"),
        web=dataclasses.replace(web, **web_overrides),
    )


def _message(gmail_id: str) -> NewsletterMessage:
    return NewsletterMessage(
        gmail_id=gmail_id,
        thread_id="thread",
        subject="Big News issue",
        sender="Daily Dispatch <editor@example.com>",
        received_at=datetime.fromisoformat("2026-10-14T08:00:00-04:00"),
        html=EMAIL_HTML,
        text="",
        gmail_url=f"https://mail.google.com/mail/u/0/#inbox/{gmail_id}",
    )


def _verify(token: str) -> str:
    if token == GOOD_TOKEN:
        return EMAIL
    raise AccessDenied("bad token")


def _seed(config: AppConfig) -> None:
    with Store(config.storage.sqlite_path) as store:
        store.add_summary(
            _message(SUMMARIZED_ID),
            SummaryItem(
                title="Big News",
                url="https://example.com/p/big-news",
                publication="Daily Dispatch",
                authors=["Ada Writer"],
                bullets=["First point.", "Second point."],
                content_access="partial",
            ),
        )
        store.log_message(SUMMARIZED_ID, status="summarized", subject="Big News issue", received_at="2026-10-14T08:00:00-04:00")
        store.log_message(SKIPPED_ID, status="skipped_no_links", subject="No links here", received_at="2026-10-14T09:00:00-04:00")


class WebUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _config(Path(self.tmp.name))
        _seed(self.config)
        self.fetched: list[str] = []
        self.fail_fetch = False

        def fetch(gmail_id: str) -> NewsletterMessage | None:
            self.fetched.append(gmail_id)
            if self.fail_fetch:
                raise RuntimeError("Gmail 500")
            return _message(gmail_id)

        self.server = build_web_server(
            self.config, address=("127.0.0.1", 0), verify=_verify, fetch=fetch, secret=SECRET
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, str] | None = None,
        token: str | None = GOOD_TOKEN,
        host: str = HOST,
        origin: str | None = ORIGIN,
    ) -> tuple[int, dict[str, str], str]:
        headers = {"Host": host, "Content-Type": "application/x-www-form-urlencoded"}
        if token is not None:
            headers["Cf-Access-Jwt-Assertion"] = token
        if origin is not None:
            headers["Origin"] = origin
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=urlencode(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        payload = response.read().decode("utf-8", errors="replace")
        result = response.status, {key.lower(): value for key, value in response.getheaders()}, payload
        conn.close()
        return result

    def csrf(self, action: str, gmail_id: str) -> str:
        return csrf_token(SECRET, EMAIL, action, gmail_id)

    def full_ids(self) -> set[str]:
        with Store(self.config.storage.sqlite_path) as store:
            return store.full_item_ids()

    def test_missing_or_invalid_jwt_is_forbidden(self) -> None:
        for token in (None, "", "forged"):
            with self.subTest(token=token):
                status, _, _ = self.request("GET", "/", token=token)
                self.assertEqual(status, 403)
        status, _, _ = self.request("POST", f"/read/{SUMMARIZED_ID}", token=None,
                                    body={"csrf": self.csrf("add", SUMMARIZED_ID)})
        self.assertEqual(status, 403)
        self.assertEqual(self.fetched, [])

    def test_wrong_host_is_rejected(self) -> None:
        status, _, _ = self.request("GET", "/", host="rss.example.com")
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/", host=f"127.0.0.1:{self.port}")
        self.assertEqual(status, 403)

    def test_pages_render_with_security_headers(self) -> None:
        for path in ("/", "/emails", "/full", "/status", "/digest/2026-10-14", f"/item/{SUMMARIZED_ID}"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn("default-src 'none'", headers["content-security-policy"])
                self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertIn("referrer-policy", headers)
                # Never show addresses: Cloudflare rewrites them to "[email protected]".
                self.assertNotIn(EMAIL, body)
                # Revalidated, never shared; no-transform keeps Cloudflare's
                # Email Obfuscation from rewriting the page.
                self.assertEqual(headers["cache-control"], "private, no-cache, no-transform")
        # The auto-submitting page, errors, and POST responses are never stored.
        _, headers, _ = self.request("GET", f"/read/{SUMMARIZED_ID}")
        self.assertEqual(headers["cache-control"], "no-store, no-transform")
        _, headers, _ = self.request("GET", f"/item/{UNKNOWN_ID}")
        self.assertEqual(headers["cache-control"], "no-store, no-transform")
        _, headers, _ = self.request("POST", f"/read/{SUMMARIZED_ID}", body={"csrf": self.csrf("add", SUMMARIZED_ID)})
        self.assertEqual(headers["cache-control"], "no-store, no-transform")
        _, _, emails = self.request("GET", "/emails")
        self.assertIn("No links here", emails)
        self.assertIn("Skipped: no links", emails)
        self.assertIn(f"https://mail.google.com/mail/u/0/#all/{SKIPPED_ID}", emails)
        self.assertIn(f'action="/read/{SKIPPED_ID}"', emails)
        _, _, full = self.request("GET", "/full")
        self.assertIn(f"https://rss.example.com/full/{full_feed_token(SECRET)}/feed.xml", full)

    def test_full_page_shows_all_three_feed_urls_with_copy_buttons(self) -> None:
        _, headers, full = self.request("GET", "/full")
        self.assertIn(f'value="https://rss.example.com/digest/{digest_feed_token(SECRET)}/feed.xml"', full)
        self.assertIn(f'value="https://rss.example.com/full/{full_feed_token(SECRET)}/feed.xml"', full)
        self.assertIn(f'value="https://rss.example.com/all/{all_feed_token(SECRET)}/feed.xml"', full)
        self.assertIn("Newsletters: Everything", full)
        for field in ("digest-url", "full-url", "all-url"):
            self.assertIn(f'data-copy="{field}"', full)
        # Removing never reaches FreshRSS's copy; the page says so.
        self.assertIn("FreshRSS keeps articles it has already fetched", full)
        nonce = re.search(r"'nonce-([^']+)'", headers["content-security-policy"])
        assert nonce is not None
        self.assertIn(f'<script nonce="{nonce.group(1)}">', full)
        self.assertIn("navigator.clipboard", full)

    def test_digest_page_lists_not_summarized_emails(self) -> None:
        _, _, index = self.request("GET", "/")
        self.assertIn("Wednesday, October 14, 2026", index)
        self.assertIn("1 newsletter, 1 not summarized", index)
        self.assertIn('<h3 class="section">Not summarized</h3>', index)
        self.assertIn(f'<a href="/item/{SKIPPED_ID}">No links here</a>', index)

    def test_reading_view_keeps_data_tables_scrollable_and_nh3_runs_last(self) -> None:
        self.request("POST", f"/read/{SUMMARIZED_ID}", body={"csrf": self.csrf("add", SUMMARIZED_ID)})
        with patch("newsletter_summary.web_ui.drop_leading_title", return_value="<p>Body</p><script>alert(1)</script>"):
            status, _, body = self.request("GET", f"/item/{SUMMARIZED_ID}")
        self.assertEqual(status, 200)
        self.assertIn("<p>Body</p>", body)
        self.assertNotIn("alert(1)", body)
        self.assertNotIn("article.post td {{ display: block", body)
        self.assertNotIn("article.post tr, article.post td", body)
        self.assertIn("article.post table { display: block; max-width: 100%; overflow-x: auto;", body)

    def test_favicon_routes_work_without_the_database(self) -> None:
        with patch("newsletter_summary.web_ui.Store", side_effect=RuntimeError("database down")):
            for path in ("/favicon.ico", "/favicon.png"):
                status, headers, _ = self.request("GET", path)
                self.assertEqual(status, 200, path)
                self.assertEqual(headers["content-type"], "image/png")
            status, _, _ = self.request("GET", "/")
            self.assertEqual(status, 500)

    def test_emails_can_be_filtered_by_newsletter_and_searched(self) -> None:
        with Store(self.config.storage.sqlite_path) as store:
            store.log_message(
                SUMMARIZED_ID,
                status="summarized",
                subject="Big News issue",
                sender="Daily Dispatch <editor@example.com>",
                received_at="2026-10-14T08:00:00-04:00",
            )
            store.log_message(
                SKIPPED_ID,
                status="skipped_no_links",
                subject="No links here",
                sender='"Other Letter" <hello@example.org>',
                received_at="2026-10-14T09:00:00-04:00",
            )
        status, _, body = self.request("GET", "/emails?feed=Other+Letter")
        self.assertEqual(status, 200)
        self.assertIn("No links here", body)
        self.assertNotIn("Big News issue", body)
        self.assertIn('<option value="Daily Dispatch">Daily Dispatch (1)</option>', body)
        self.assertNotIn("example.org", body)
        _, _, body = self.request("GET", "/emails?q=big+news")
        self.assertIn(f'<a href="/item/{SUMMARIZED_ID}">Big News</a>', body)
        self.assertNotIn("No links here", body)
        self.assertIn("Add to reading list", body)
        # Search also covers the summary: key points, publication, and authors.
        for query in ("second+point", "daily+dispatch", "ada+writer"):
            _, _, body = self.request("GET", f"/emails?q={query}")
            self.assertIn("1 email<", body, query)
            self.assertIn(f"/item/{SUMMARIZED_ID}", body, query)

    def test_get_read_has_no_side_effects_and_serves_a_csrf_form_for_its_script(self) -> None:
        """GET /read changes nothing; it serves the POST form and the script that submits it.

        This checks the page's structure (form, token, nonce-allowed script
        that targets that form). Whether the script waits for visibility is
        browser behavior, which this server-side test cannot exercise.
        """
        status, headers, body = self.request("GET", f"/read/{SUMMARIZED_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(self.fetched, [])
        self.assertEqual(self.full_ids(), set())
        form = re.search(r'<form id="add-form" method="post" action="/read/([0-9a-f]+)">(.*?)</form>', body, re.S)
        assert form is not None
        self.assertEqual(form.group(1), SUMMARIZED_ID)
        self.assertIn(f'name="csrf" value="{self.csrf("add", SUMMARIZED_ID)}"', form.group(2))
        self.assertIn("Add to reading list", form.group(2))
        nonce = re.search(r"'nonce-([^']+)'", headers["content-security-policy"])
        assert nonce is not None
        scripts = re.findall(r'<script nonce="([^"]+)">(.*?)</script>', body, re.S)
        self.assertEqual([found for found, _ in scripts], [nonce.group(1)])
        self.assertIn('getElementById("add-form")', scripts[0][1])
        self.assertIn("form.submit()", scripts[0][1])

    def test_unknown_or_invalid_ids_are_not_found_without_calling_gmail(self) -> None:
        for path in (f"/read/{UNKNOWN_ID}", "/read/NOT-HEX", "/read/abc", f"/item/{UNKNOWN_ID}"):
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path)
                self.assertEqual(status, 404)
        status, _, _ = self.request("POST", f"/read/{UNKNOWN_ID}", body={"csrf": self.csrf("add", UNKNOWN_ID)})
        self.assertEqual(status, 404)
        self.assertEqual(self.fetched, [])

    def test_post_requires_origin_and_csrf(self) -> None:
        cases: list[tuple[str | None, dict[str, str]]] = [
            (None, {"csrf": self.csrf("add", SUMMARIZED_ID)}),
            ("null", {"csrf": self.csrf("add", SUMMARIZED_ID)}),
            ("https://evil.example", {"csrf": self.csrf("add", SUMMARIZED_ID)}),
            (ORIGIN, {}),
            (ORIGIN, {"csrf": "0" * 64}),
            (ORIGIN, {"csrf": self.csrf("remove", SUMMARIZED_ID)}),
            (ORIGIN, {"csrf": self.csrf("add", SKIPPED_ID)}),
            (ORIGIN, {"csrf": csrf_token(SECRET, "other@example.com", "add", SUMMARIZED_ID)}),
        ]
        for origin, body in cases:
            with self.subTest(origin=origin, body=body):
                status, _, _ = self.request("POST", f"/read/{SUMMARIZED_ID}", origin=origin, body=body)
                self.assertEqual(status, 403)
        self.assertEqual(self.fetched, [])
        self.assertEqual(self.full_ids(), set())

    def test_add_is_idempotent_and_shows_the_sanitized_article(self) -> None:
        body = {"csrf": self.csrf("add", SUMMARIZED_ID)}
        status, headers, _ = self.request("POST", f"/read/{SUMMARIZED_ID}", body=body)
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], f"/item/{SUMMARIZED_ID}?added=1")
        status, headers, _ = self.request("POST", f"/read/{SUMMARIZED_ID}", body=body)
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], f"/item/{SUMMARIZED_ID}?added=1")
        self.assertEqual(self.fetched, [SUMMARIZED_ID])
        self.assertEqual(self.full_ids(), {SUMMARIZED_ID})

        status, _, page = self.request("GET", f"/item/{SUMMARIZED_ID}?added=1")
        self.assertEqual(status, 200)
        self.assertIn("Added to your reading list", page)
        self.assertNotIn("<h1>Big News</h1>", page)
        # Images load through the signed proxy on the public host, never from the sender.
        self.assertRegex(page, r'<img [^>]*src="https://rss\.example\.com/img/[0-9a-f]{32}/[A-Za-z0-9_-]+"')
        self.assertNotIn("cdn.example.com", page)
        self.assertIn('href="https://example.com/story"', page)
        self.assertIn("Excerpt only", page)
        self.assertIn("https://example.com/p/big-news", page)
        self.assertIn(f"https://mail.google.com/mail/u/0/#all/{SUMMARIZED_ID}", page)
        self.assertNotIn("alert(1)", page)
        self.assertNotIn("open.gif", page)
        self.assertNotIn("preview text", page)

        # GET /read for an item already in the feed redirects instead of re-adding.
        status, headers, _ = self.request("GET", f"/read/{SUMMARIZED_ID}")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], f"/item/{SUMMARIZED_ID}")
        _, _, digests = self.request("GET", "/")
        self.assertIn(f'href="/item/{SUMMARIZED_ID}">On reading list', digests)

    def test_email_without_summary_can_be_added(self) -> None:
        status, _, _ = self.request("POST", f"/read/{SKIPPED_ID}", body={"csrf": self.csrf("add", SKIPPED_ID)})
        self.assertEqual(status, 303)
        with Store(self.config.storage.sqlite_path) as store:
            item = store.get_full_item(SKIPPED_ID)
        assert item is not None
        self.assertEqual(item.title, "Big News issue")
        self.assertEqual(item.publication, "Daily Dispatch")
        self.assertEqual(item.url, "")

    def test_add_and_remove_ask_the_reader_to_refresh(self) -> None:
        calls: list[str] = []
        self.server.refresh_reader = lambda: calls.append("refresh")
        csrf = csrf_token(SECRET, EMAIL, "add", SUMMARIZED_ID)
        status, _, _ = self.request("POST", f"/read/{SUMMARIZED_ID}", body={"csrf": csrf})
        self.assertEqual(status, 303)
        self.assertEqual(calls, ["refresh"])
        _, _, page = self.request("GET", f"/item/{SUMMARIZED_ID}?added=1")
        self.assertIn("in a moment", page)
        csrf = csrf_token(SECRET, EMAIL, "remove", SUMMARIZED_ID)
        self.request("POST", f"/item/{SUMMARIZED_ID}/remove", body={"csrf": csrf})
        self.assertEqual(calls, ["refresh", "refresh"])

    def test_reader_refresher_needs_a_url_and_never_logs_it(self) -> None:
        from newsletter_summary import web_ui

        with patch.dict(os.environ, {"FRESHRSS_REFRESH_URL": ""}):
            self.assertIsNone(web_ui.reader_refresher(self.config))
        url = "https://reader.example.com/i/?c=feed&a=actualize&id=7&user=me&token=sekret"
        with (
            patch.dict(os.environ, {"FRESHRSS_REFRESH_URL": url}),
            patch.object(web_ui, "REFRESH_DELAYS_SECONDS", (0,)),
            patch.object(web_ui.requests, "get", side_effect=web_ui.requests.ConnectionError(url)) as get,
            self.assertLogs(level="WARNING") as logs,
        ):
            refresh = web_ui.reader_refresher(self.config)
            assert refresh is not None
            refresh()
            for _ in range(50):
                if get.called and logs.output:
                    break
                time.sleep(0.02)
        get.assert_called_once_with(url, timeout=30)
        self.assertNotIn("sekret", "\n".join(logs.output))

    def test_gmail_failure_shows_error_and_stores_nothing(self) -> None:
        self.fail_fetch = True
        status, _, page = self.request("POST", f"/read/{SUMMARIZED_ID}", body={"csrf": self.csrf("add", SUMMARIZED_ID)})
        self.assertEqual(status, 502)
        self.assertIn(f"https://mail.google.com/mail/u/0/#all/{SUMMARIZED_ID}", page)
        self.assertNotIn("Gmail 500", page)
        self.assertEqual(self.full_ids(), set())

    def test_remove_works_and_needs_csrf(self) -> None:
        self.request("POST", f"/read/{SUMMARIZED_ID}", body={"csrf": self.csrf("add", SUMMARIZED_ID)})
        status, _, _ = self.request("POST", f"/item/{SUMMARIZED_ID}/remove", body={"csrf": self.csrf("add", SUMMARIZED_ID)})
        self.assertEqual(status, 403)
        self.assertEqual(self.full_ids(), {SUMMARIZED_ID})
        status, headers, _ = self.request(
            "POST", f"/item/{SUMMARIZED_ID}/remove", body={"csrf": self.csrf("remove", SUMMARIZED_ID)}
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/full?removed=1")
        self.assertEqual(self.full_ids(), set())
        _, _, page = self.request("GET", "/full?removed=1")
        self.assertIn("Removed. Removing an item takes it off this list", page)
        # The reading view still works for any processed email; it now offers to add it again.
        status, _, page = self.request("GET", f"/item/{SUMMARIZED_ID}")
        self.assertEqual(status, 200)
        self.assertIn(f'action="/read/{SUMMARIZED_ID}"', page)


    def test_reading_view_opens_any_processed_email_and_stores_what_gmail_returned(self) -> None:
        status, _, page = self.request("GET", f"/item/{SKIPPED_ID}")
        self.assertEqual(status, 200)
        self.assertIn("Hello <strong>reader</strong>", page)
        self.assertIn(f'action="/read/{SKIPPED_ID}"', page)
        self.assertEqual(self.fetched, [SKIPPED_ID])
        # Stored on first view: the second view is instant, with no Gmail call.
        self.request("GET", f"/item/{SKIPPED_ID}")
        self.assertEqual(self.fetched, [SKIPPED_ID])
        with Store(self.config.storage.sqlite_path) as store:
            self.assertIsNotNone(store.get_content(SKIPPED_ID))

    def test_stored_content_is_read_and_added_without_gmail(self) -> None:
        from newsletter_summary.email_content import save_message

        with Store(self.config.storage.sqlite_path) as store:
            save_message(store, _message(SUMMARIZED_ID))
        status, _, page = self.request("GET", f"/item/{SUMMARIZED_ID}")
        self.assertEqual(status, 200)
        self.assertIn("Key points", page)
        self.assertIn("Daily Dispatch", page)
        status, _, _ = self.request("POST", f"/read/{SUMMARIZED_ID}", body={"csrf": self.csrf("add", SUMMARIZED_ID)})
        self.assertEqual(status, 303)
        self.assertEqual(self.fetched, [])
        with Store(self.config.storage.sqlite_path) as store:
            item = store.get_full_item(SUMMARIZED_ID)
        assert item is not None
        # Membership and metadata only; the body lives in the content store.
        self.assertEqual(item.content_html, "")
        self.assertEqual(item.bullets, ["First point.", "Second point."])

    def test_reading_list_copy_from_before_the_content_store_is_used_without_gmail(self) -> None:
        from newsletter_summary.store import FullItem

        with Store(self.config.storage.sqlite_path) as store:
            store.add_full_item(
                FullItem(
                    gmail_id=SUMMARIZED_ID, title="Big News", url="", publication="Daily Dispatch", authors=[],
                    content_access="full", source_gmail_url="", bullets=[], content_html="<p>Legacy body</p>",
                    email_received_at="2026-10-14T12:00:00+00:00", added_at="2026-10-14T13:00:00+00:00",
                )
            )
        status, _, page = self.request("GET", f"/item/{SUMMARIZED_ID}")
        self.assertEqual(status, 200)
        self.assertIn("<p>Legacy body</p>", page)
        self.assertEqual(self.fetched, [])
        self.assertIn("Remove from reading list", page)

    def test_item_without_a_body_or_gmail_shows_an_error(self) -> None:
        self.fail_fetch = True
        status, _, page = self.request("GET", f"/item/{SKIPPED_ID}")
        self.assertEqual(status, 502)
        self.assertIn(f"#all/{SKIPPED_ID}", page)
        self.assertNotIn("Gmail 500", page)

    def test_digest_day_pages(self) -> None:
        status, _, page = self.request("GET", "/digest/2026-10-14")
        self.assertEqual(status, 200)
        self.assertIn("<h1>Wednesday, October 14, 2026</h1>", page)
        self.assertIn(f'<h3><a href="/item/{SUMMARIZED_ID}">Big News</a></h3>', page)
        self.assertIn('<h2 class="section">Not summarized</h2>', page)
        for path in ("/digest/2026-10-13", "/digest/not-a-date", "/digest/2099-01-01"):
            self.assertEqual(self.request("GET", path)[0], 404, path)
        _, _, index = self.request("GET", "/")
        self.assertIn('<h2><a href="/digest/2026-10-14">Wednesday, October 14, 2026</a></h2>', index)

    def test_index_shows_today_so_far_before_it_is_published(self) -> None:
        with Store(self.config.storage.sqlite_path) as store:
            message = dataclasses.replace(
                _message("18c0ffee00000010"), subject="Fresh", received_at=datetime.now(self.config.gmail.tzinfo)
            )
            store.add_summary(
                message,
                SummaryItem(title="Fresh today", url="https://example.com/fresh", publication="Morning Note",
                            authors=[], bullets=["New."], content_access="full"),
            )
        _, _, index = self.request("GET", "/")
        today = index[index.index('<section class="today"') : index.index("</section>")]
        self.assertIn("Today so far", today)
        self.assertIn("Fresh today", today)
        self.assertIn("1 newsletter so far", today)
        self.assertEqual(index.count("Fresh today"), 1)
        # Page 2 is for older digests only.
        _, _, older = self.request("GET", "/?page=2")
        self.assertNotIn("Today so far", older)

    def test_status_page_reports_runs_failures_pings_and_migrations(self) -> None:
        from newsletter_summary.status import record_refresh

        with Store(self.config.storage.sqlite_path) as store:
            for _ in range(3):
                store.log_message("18c0ffee00000020", status="summary_failed", subject="Broken issue",
                                  sender="X <x@example.com>", detail="RuntimeError: boom", count_attempt=True)
            record_refresh(store, ok=False, detail="ConnectTimeout")
        status, _, page = self.request("GET", "/status")
        self.assertEqual(status, 200)
        self.assertIn("No run has ever succeeded.", page)
        self.assertIn("1 email(s) were given up on", page)
        self.assertIn("Broken issue", page)
        self.assertIn("<strong>given up</strong>", page)
        self.assertIn("failed (ConnectTimeout)", page)
        self.assertIn("The last FreshRSS refresh ping failed.", page)
        self.assertIn("<th scope=row>Stored email content</th><td>installed</td>", page)
        with Store(self.config.storage.sqlite_path) as store:
            store.connection.execute("drop table email_content")
        _, _, page = self.request("GET", "/status")
        self.assertIn("missing: apply deploy/sql/2026-11-24-email-content.sql", page)

    def test_navigation_marks_the_current_page_and_csp_allows_only_proxied_images(self) -> None:
        _, headers, page = self.request("GET", "/emails")
        self.assertIn('<nav aria-label="Main">', page)
        self.assertIn('<a href="/emails" aria-current="page">Emails</a>', page)
        self.assertEqual(page.count('aria-current="page">'), 1)
        self.assertIn("img-src 'self' data: https://rss.example.com;", headers["content-security-policy"])
        self.assertIn("min-height: var(--tap)", page)
        self.assertIn("--tap: 44px", page)

    def test_refresh_results_are_recorded_without_the_url(self) -> None:
        from newsletter_summary import web_ui

        recorded: list[tuple[bool, str]] = []
        url = "https://reader.example.com/i/?c=feed&a=actualize&token=sekret"
        with (
            patch.dict(os.environ, {"FRESHRSS_REFRESH_URL": url}),
            patch.object(web_ui, "REFRESH_DELAYS_SECONDS", (0,)),
            patch.object(web_ui.requests, "get", return_value=type("R", (), {"status_code": 200})()),
        ):
            refresh = web_ui.reader_refresher(self.config, record=lambda ok, detail: recorded.append((ok, detail)))
            assert refresh is not None
            refresh()
            for _ in range(50):
                if recorded:
                    break
                time.sleep(0.02)
        self.assertEqual(recorded, [(True, "HTTP 200")])


class WebListenerConfigTests(unittest.TestCase):
    def test_listener_fails_closed_without_access_settings_or_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for overrides, secret in [
                ({"access_team_domain": ""}, SECRET),
                ({"access_aud": ""}, SECRET),
                ({"allowed_emails": ()}, SECRET),
                ({"public_url": ""}, SECRET),
                ({}, ""),
            ]:
                with self.subTest(overrides=overrides, secret=bool(secret)), self.assertRaises(ValueError):
                    build_web_server(_config(root, **overrides), address=("127.0.0.1", 0), secret=secret)
            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(start_web_listener(_config(root)))
            self.assertIsNone(start_web_listener(_config(root, enabled=False)))

    def test_dev_flag_allows_running_without_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory), access_team_domain="", access_aud="", dev_insecure_no_auth=True)
            server = build_web_server(config, address=("127.0.0.1", 0), secret=SECRET, fetch=lambda _id: None)
            try:
                self.assertIsInstance(server, WebHTTPServer)
                self.assertIsNone(server.verify)
            finally:
                server.server_close()


class OriginalUrlTests(unittest.TestCase):
    def test_fallback_links_are_not_kept_as_the_original(self) -> None:
        from newsletter_summary.render import original_url as _original_url

        self.assertEqual(_original_url("https://example.com/p/x", ORIGIN), "https://example.com/p/x")
        self.assertEqual(_original_url(f"{ORIGIN}/read/{SUMMARIZED_ID}", ORIGIN), "")
        self.assertEqual(_original_url(f"https://mail.google.com/mail/u/0/#all/{SUMMARIZED_ID}", ORIGIN), "")
        self.assertEqual(_original_url("", ORIGIN), "")


class _PublicListener(unittest.TestCase):
    """The public feed listener with WEB_SECRET set and the store seeded."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _config(Path(self.tmp.name), full_feed_max_items=2)
        _seed(self.config)
        self.env = patch.dict(os.environ, {"WEB_SECRET": SECRET})
        self.env.start()
        self.server = PlatformHTTPServer(("127.0.0.1", 0), self.config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.stop()
        self.tmp.cleanup()

    def get(self, path: str) -> tuple[int, str]:
        status, _, payload = self.fetch(path)
        return status, payload.decode("utf-8", errors="replace")

    def fetch(self, path: str, headers: dict[str, str] | None = None, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, payload

    def add_items(self) -> None:
        from newsletter_summary.full_content import sanitize_email
        from newsletter_summary.store import FullItem

        with Store(self.config.storage.sqlite_path) as store:
            for index, gmail_id in enumerate([SKIPPED_ID, SUMMARIZED_ID, UNKNOWN_ID]):
                store.add_full_item(
                    FullItem(
                        gmail_id=gmail_id,
                        title=f"Item {index}",
                        url="https://example.com/p/big-news" if gmail_id == SUMMARIZED_ID else "",
                        publication="Daily Dispatch",
                        authors=["Ada Writer"],
                        content_access="partial" if gmail_id == SUMMARIZED_ID else "full",
                        source_gmail_url=f"https://mail.google.com/mail/u/0/#all/{gmail_id}",
                        bullets=["First point.", "Second <point>."],
                        content_html=sanitize_email(EMAIL_HTML, "").html,
                        email_received_at="2026-10-14T08:00:00-04:00",
                        added_at=f"2026-10-1{index + 5}T12:00:00+00:00",
                    )
                )

class FullFeedTests(_PublicListener):
    def test_wrong_token_is_not_found(self) -> None:
        self.add_items()
        for path in ("/full/wrong/feed.xml", f"/full/{full_feed_token('other')}/feed.xml", "/full/feed.xml",
                     f"/full/{full_feed_token(SECRET)}/other.xml"):
            with self.subTest(path=path):
                status, _ = self.get(path)
                self.assertEqual(status, 404)
        with patch.dict(os.environ, {"WEB_SECRET": ""}):
            status, _ = self.get(f"/full/{full_feed_token('')}/feed.xml")
            self.assertEqual(status, 404)

    def test_right_token_returns_rss_with_full_content_and_images(self) -> None:
        self.add_items()
        status, payload = self.get(f"/full/{full_feed_token(SECRET)}/feed.xml")
        self.assertEqual(status, 200)
        root = ET.fromstring(payload)
        self.assertEqual(root.findtext("./channel/title"), "Newsletters: Reading List")
        items = root.findall("./channel/item")
        # Newest added first, capped at web.full_feed_max_items.
        self.assertEqual([item.findtext("title") for item in items], ["Item 2", "Item 1"])
        newest, summarized = items
        self.assertEqual(newest.findtext("guid"), f"newsletter-full:{UNKNOWN_ID}")
        guid = newest.find("guid")
        assert guid is not None
        self.assertEqual(guid.get("isPermaLink"), "false")
        self.assertEqual(newest.findtext("link"), f"https://mail.google.com/mail/u/0/#all/{UNKNOWN_ID}")
        self.assertEqual(summarized.findtext("link"), "https://example.com/p/big-news")
        self.assertEqual(summarized.findtext("pubDate"), "Fri, 16 Oct 2026 12:00:00 +0000")
        content = summarized.findtext(f"{CONTENT_NS}encoded") or ""
        self.assertRegex(content, r'<img [^>]*src="https://rss\.example\.com/img/[0-9a-f]{32}/[A-Za-z0-9_-]+"')
        self.assertNotIn("cdn.example.com", content)
        self.assertIn("<h1>Big News</h1>", content)
        self.assertIn("Excerpt only", content)
        self.assertNotIn("<script", content)
        description = summarized.findtext("description") or ""
        self.assertIn("<li>Second &lt;point&gt;.</li>", description)
        self.assertIn("Excerpt only", description)

    def test_feeds_have_distinct_public_icons_and_home_pages(self) -> None:
        status, payload = self.get(f"/full/{full_feed_token(SECRET)}/feed.xml")
        channel = ET.fromstring(payload).find("channel")
        assert channel is not None
        self.assertEqual(channel.findtext("link"), "https://rss.example.com/newsletters/full/")
        self.assertEqual(channel.findtext("image/url"), "https://rss.example.com/newsletters/icons/full.png")
        self.assertEqual(channel.findtext("{http://www.w3.org/2005/Atom}icon"), "https://rss.example.com/newsletters/icons/full.png")
        for kind in ("digest", "full"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", f"/newsletters/icons/{kind}.png")
            response = conn.getresponse()
            body = response.read()
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "image/png")
            self.assertTrue(body.startswith(b"\x89PNG"))
            status, page = self.get(f"/newsletters/{kind}/")
            self.assertEqual(status, 200)
            self.assertIn(f'<link rel="icon" type="image/png" href="https://rss.example.com/newsletters/icons/{kind}.png">', page)
        for path in ("/newsletters/icons/other.png", "/newsletters/icons/../config.yaml", "/newsletters/digest"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_feeds_and_home_pages_survive_database_outages_for_static_routes(self) -> None:
        with patch("newsletter_summary.platform_server.Store", side_effect=RuntimeError("database down")):
            self.assertEqual(self.get("/newsletters/icons/digest.png")[0], 200)
            self.assertEqual(self.get("/newsletters/full/")[0], 200)
            self.assertEqual(self.get("/favicon.ico")[0], 200)
            self.assertEqual(self.get(f"/digest/{digest_feed_token(SECRET)}/feed.xml")[0], 500)

    def test_icon_paths_must_name_the_png(self) -> None:
        for path in ("/newsletters/icons/digest", "/newsletters/icons/full", "/newsletters/icons/digest.png.png"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_digest_page_title_comes_from_config(self) -> None:
        _, page = self.get("/newsletters/digest/")
        self.assertIn("<title>Newsletter Highlights</title>", page)

    def test_token_is_masked_in_request_logs(self) -> None:
        token = full_feed_token(SECRET)
        with self.assertLogs(level="INFO") as logs:
            self.get(f"/full/{token}/feed.xml")
        self.assertFalse(any(token in line for line in logs.output))
        self.assertEqual(mask_full_feed_token(f'"GET /full/{token}/feed.xml HTTP/1.1"'),
                         '"GET /full/<redacted>/feed.xml HTTP/1.1"')


class DigestFeedTests(_PublicListener):
    def url(self, secret: str = SECRET) -> str:
        return f"/digest/{digest_feed_token(secret)}/feed.xml"

    def test_old_feed_url_is_gone_without_a_redirect(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/feed.xml")
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 404)
        self.assertIsNone(response.getheader("Location"))
        for path in ("/", "/reader", "/opml", "/accounts/ClientLogin", "/reader/api/0/subscription/list"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_wrong_or_missing_token_is_not_found(self) -> None:
        for path in ("/digest/wrong/feed.xml", self.url("other"), "/digest/feed.xml",
                     f"/digest/{digest_feed_token(SECRET)}/other.xml", f"/digest/{full_feed_token(SECRET)}/feed.xml"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)
        with patch.dict(os.environ, {"WEB_SECRET": ""}):
            self.assertEqual(self.get(self.url(""))[0], 404)

    def test_digest_is_rendered_from_the_database_with_a_stale_run_alert(self) -> None:
        status, payload = self.get(self.url())
        self.assertEqual(status, 200)
        channel = ET.fromstring(payload).find("channel")
        assert channel is not None
        self.assertEqual(channel.findtext("title"), "Newsletter Highlights")
        self.assertEqual(channel.findtext("link"), "https://rss.example.com/newsletters/digest/")
        self_link = channel.find("{http://www.w3.org/2005/Atom}link")
        assert self_link is not None
        self.assertEqual(self_link.get("href"), f"https://rss.example.com{self.url()}")
        items = channel.findall("item")
        # No run has ever succeeded here, so an alert comes first.
        self.assertEqual(items[0].findtext("guid"), "newsletter-digest-alert:stale:never")
        self.assertEqual(items[1].findtext("guid"), "newsletter-digest:2026-10-14")
        description = items[1].findtext("description") or ""
        self.assertIn("Big News", description)
        self.assertIn(f'<a href="{ORIGIN}/read/{SUMMARIZED_ID}">Read in full</a>', description)
        self.assertIn("No links here", description)

    def test_no_alert_after_a_recent_successful_run(self) -> None:
        from newsletter_summary.run_state import RunState

        state = RunState(self.config)
        state.start(datetime.fromisoformat("2026-10-14T00:00:00+00:00"), datetime.now().astimezone())
        state.finish(success=True)
        _, payload = self.get(self.url())
        guids = [item.findtext("guid") for item in ET.fromstring(payload).findall("./channel/item")]
        self.assertEqual(guids, ["newsletter-digest:2026-10-14"])

    def test_digest_token_is_masked_in_logs(self) -> None:
        token = digest_feed_token(SECRET)
        with self.assertLogs(level="INFO") as logs:
            self.get(self.url())
        self.assertFalse(any(token in line for line in logs.output))
        self.assertEqual(mask_feed_tokens(f"GET /digest/{token}/feed.xml"), "GET /digest/<redacted>/feed.xml")


if __name__ == "__main__":
    unittest.main()


class AllFeedListenerTests(_PublicListener):
    def url(self, secret: str = SECRET) -> str:
        return f"/all/{all_feed_token(secret)}/feed.xml"

    def test_only_the_right_token_is_served(self) -> None:
        for path in ("/all/wrong/feed.xml", self.url("other"), f"/all/{digest_feed_token(SECRET)}/feed.xml"):
            self.assertEqual(self.get(path)[0], 404, path)
        with patch.dict(os.environ, {"WEB_SECRET": ""}):
            self.assertEqual(self.get(self.url(""))[0], 404)

    def test_every_summarized_newsletter_with_stored_full_text_and_proxied_images(self) -> None:
        from newsletter_summary.email_content import save_message

        with Store(self.config.storage.sqlite_path) as store:
            save_message(store, _message(SUMMARIZED_ID))
        status, payload = self.get(self.url())
        self.assertEqual(status, 200)
        channel = ET.fromstring(payload).find("channel")
        assert channel is not None
        self.assertEqual(channel.findtext("title"), "Newsletters: Everything")
        items = channel.findall("item")
        self.assertEqual([item.findtext("guid") for item in items], [f"newsletter-all:{SUMMARIZED_ID}"])
        self.assertEqual(items[0].findtext("link"), f"{ORIGIN}/item/{SUMMARIZED_ID}")
        body = items[0].findtext(f"{CONTENT_NS}encoded") or ""
        self.assertIn("Hello <strong>reader</strong>", body)
        self.assertIn(f'<a href="{ORIGIN}/read/{SUMMARIZED_ID}">Add to reading list</a>', body)
        self.assertRegex(body, r'src="https://rss\.example\.com/img/[0-9a-f]{32}/')
        self.assertNotIn("cdn.example.com", body)

    def test_home_page_icon_and_masked_logs(self) -> None:
        status, page = self.get("/newsletters/all/")
        self.assertEqual(status, 200)
        self.assertIn("<title>Newsletters: Everything</title>", page)
        status, headers, icon = self.fetch("/newsletters/icons/all.png")
        self.assertEqual((status, headers["content-type"]), (200, "image/png"))
        self.assertTrue(icon.startswith(b"\x89PNG"))
        token = all_feed_token(SECRET)
        with self.assertLogs(level="INFO") as logs:
            self.get(self.url())
        self.assertFalse(any(token in line for line in logs.output))


class ConditionalFeedTests(_PublicListener):
    def test_every_feed_supports_etag_last_modified_and_304(self) -> None:
        from newsletter_summary.run_state import RunState

        # A recent success, so the digest has no clock-dated alert item.
        state = RunState(self.config)
        state.start(datetime.fromisoformat("2026-10-14T00:00:00+00:00"), datetime.now().astimezone())
        state.finish(success=True)
        self.add_items()
        for path in (f"/digest/{digest_feed_token(SECRET)}/feed.xml", f"/full/{full_feed_token(SECRET)}/feed.xml",
                     f"/all/{all_feed_token(SECRET)}/feed.xml"):
            with self.subTest(path=path.split("/")[1]):
                status, headers, body = self.fetch(path)
                self.assertEqual(status, 200)
                etag = headers["etag"]
                self.assertRegex(etag, r'^"[0-9a-f]{32}"$')
                self.assertIn("GMT", headers["last-modified"])
                # Unchanged: the same ETag on the next request.
                self.assertEqual(self.fetch(path)[1]["etag"], etag)
                status, headers, body = self.fetch(path, {"If-None-Match": etag})
                self.assertEqual((status, body), (304, b""))
                self.assertEqual(headers["etag"], etag)
                # Cloudflare weakens ETags on compressed responses.
                self.assertEqual(self.fetch(path, {"If-None-Match": f"W/{etag}"})[0], 304)
                self.assertEqual(self.fetch(path, {"If-None-Match": '"stale"'})[0], 200)
                last_modified = self.fetch(path)[1]["last-modified"]
                self.assertEqual(self.fetch(path, {"If-Modified-Since": last_modified})[0], 304)
                status, headers, body = self.fetch(path, method="HEAD")
                self.assertEqual((status, body), (200, b""))


class ImageRouteTests(_PublicListener):
    def setUp(self) -> None:
        super().setUp()
        self.calls: list[str] = []

        def fetch(url: str) -> Image:
            self.calls.append(url)
            if "missing" in url:
                raise ImageFetchError("HTTP 404")
            return Image(b"\x89PNG\r\n\x1a\n" + b"0" * 16, "image/png")

        self.server.images = ImageProxy(
            ImageCache(Path(self.tmp.name) / "images", 1_000_000), max_bytes=1000, fetch=fetch
        )

    def path(self, url: str) -> str:
        return proxied_image_url("", SECRET, url)

    def test_signed_urls_are_fetched_cached_and_served_safely(self) -> None:
        path = self.path("https://cdn.example.com/hero.jpg?token=sekret")
        with self.assertLogs(level="INFO") as logs:
            status, headers, body = self.fetch(path)
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertEqual(headers["content-security-policy"], "default-src 'none'; sandbox")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertFalse(any("sekret" in line or path.rsplit("/", 1)[1] in line for line in logs.output))
        self.fetch(path)
        self.assertEqual(self.calls, ["https://cdn.example.com/hero.jpg?token=sekret"])

    def test_unsigned_or_tampered_urls_are_never_fetched(self) -> None:
        good = self.path("https://cdn.example.com/hero.jpg")
        signature, encoded = good.split("/")[2:4]
        other = proxied_image_url("", "other-secret", "https://cdn.example.com/hero.jpg")
        for path in (f"/img/{'0' * 32}/{encoded}", other, f"/img/{signature}", f"/img/{signature}/{encoded}/x",
                     self.path("http://169.254.169.254:8080/latest").replace("/img/", "/img/x")):
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path)[0], 404)
        with patch.dict(os.environ, {"WEB_SECRET": ""}):
            self.assertEqual(self.fetch(good)[0], 404)
        self.assertEqual(self.calls, [])

    def test_failed_fetches_are_404_with_a_short_cache(self) -> None:
        status, headers, _ = self.fetch(self.path("https://cdn.example.com/missing.png"))
        self.assertEqual(status, 404)
        self.assertEqual(headers["cache-control"], "public, max-age=600")

    def test_proxy_off_serves_nothing_and_leaves_images_direct(self) -> None:
        from newsletter_summary.config import ImagesConfig
        from newsletter_summary.image_proxy import image_rewriter

        self.server.config = dataclasses.replace(self.config, images=ImagesConfig(proxy=False))
        self.assertEqual(self.fetch(self.path("https://cdn.example.com/hero.jpg"))[0], 404)
        self.assertIsNone(image_rewriter(self.server.config, SECRET))
