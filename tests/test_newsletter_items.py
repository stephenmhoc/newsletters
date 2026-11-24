from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from newsletter_summary.digest import build_digests, digest_window
from newsletter_summary.email_extract import ExtractedLink
from newsletter_summary.gmail_client import NewsletterMessage
from newsletter_summary.openrouter import SummaryItem, summarize_message
from newsletter_summary.rss import render_digest_feed
from newsletter_summary.store import Store

EASTERN = ZoneInfo("America/New_York")


def _message(gmail_id: str = "gmail-123", received_at: datetime | None = None) -> NewsletterMessage:
    return NewsletterMessage(
        gmail_id=gmail_id,
        thread_id=f"thread-{gmail_id}",
        subject="[Daily] Issue #42: Big news",
        sender="Daily Dispatch <editor@example.com>",
        received_at=received_at or datetime(2026, 6, 11, 9, 0, tzinfo=UTC),
        html="",
        text="",
        gmail_url=f"https://mail.google.com/mail/u/0/#inbox/{gmail_id}",
    )


def _summary(title: str = "Big news", **overrides) -> SummaryItem:
    values = {
        "title": title,
        "url": "https://example.com/p/big-news",
        "publication": "Daily Dispatch",
        "authors": ["Ada Writer", "Bo Editor"],
        "bullets": ["First key point.", "Second key point."],
        "content_access": "full",
    }
    values.update(overrides)
    return SummaryItem(**values)


def _response(content: dict) -> Mock:
    response = Mock(status_code=200)
    response.json.return_value = {"choices": [{"message": {"content": json.dumps(content)}}]}
    return response


def _summarize(response: Mock, *, links: list[ExtractedLink] | None = None, web_links=None, **kwargs):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
        "newsletter_summary.openrouter.time.sleep"
    ), patch("newsletter_summary.openrouter.requests.post", return_value=response) as post:
        item = summarize_message(
            api_key_env="OPENROUTER_API_KEY",
            keychain_service=None,
            keychain_account=None,
            model="test-model",
            max_input_chars=1000,
            app_name="Newsletter Summary",
            site_url="http://localhost",
            base_prompt="Summarize newsletters.",
            message=_message(),
            body_markdown="A newsletter body.",
            links=links or [],
            web_links=web_links,
            **kwargs,
        )
    return item, post


class SummaryContractTest(unittest.TestCase):
    def test_summary_has_title_publication_authors_link_and_bullets(self) -> None:
        item, _ = _summarize(
            _response(
                {
                    "title": "Big news",
                    "publication": "Daily Dispatch",
                    "authors": ["Ada Writer", " Bo  Editor "],
                    "url": "https://example.com/p/big-news",
                    "content_access": "partial",
                    "bullets": ["- First  point.", "Second point.", "Third point.", "Fourth point.", "  "],
                }
            ),
            web_links=[ExtractedLink(url="https://example.com/p/big-news", text="View in browser")],
        )

        assert item is not None
        self.assertEqual(item.title, "Big news")
        self.assertEqual(item.publication, "Daily Dispatch")
        self.assertEqual(item.authors, ["Ada Writer", "Bo Editor"])
        self.assertEqual(item.url, "https://example.com/p/big-news")
        self.assertEqual(item.content_access, "partial")
        self.assertEqual(item.bullets, ["First point.", "Second point.", "Third point."])

    def test_link_not_found_in_the_email_falls_back_to_gmail(self) -> None:
        item, _ = _summarize(
            _response({"title": "Big news", "url": "https://invented.example/article", "bullets": ["A point."]}),
            links=[ExtractedLink(url="https://example.com/story", text="Story")],
        )

        assert item is not None
        self.assertEqual(item.url, _message().gmail_url)
        self.assertEqual(item.authors, [])
        self.assertEqual(item.publication, "Daily Dispatch")

    def test_response_without_bullets_returns_no_summary(self) -> None:
        item, _ = _summarize(_response({"title": "Big news", "bullets": []}))
        self.assertIsNone(item)

    def test_request_uses_strict_json_schema_without_temperature(self) -> None:
        _, post = _summarize(_response({"title": "Big news", "bullets": ["A point."]}))

        payload = post.call_args.kwargs["json"]
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertTrue(payload["provider"]["require_parameters"])

    def test_prompt_handles_roundups_and_paid_previews_without_rating(self) -> None:
        _, post = _summarize(
            _response({"bullets": []}),
            web_links=[ExtractedLink(url="https://example.com/p/big-news", text="View in browser")],
        )

        system_prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
        user_prompt = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("recap, roundup, and index-style editions", system_prompt)
        self.assertIn("content_access: partial when", system_prompt)
        self.assertIn("bullets: one to 3 bullets", system_prompt)
        self.assertIn("The bullets replace reading the newsletter", system_prompt)
        self.assertIn("authors:", system_prompt)
        self.assertNotIn("recommendation", system_prompt)
        self.assertNotIn("worth reading", system_prompt)
        self.assertIn("View in browser: https://example.com/p/big-news", user_prompt)


class OpenRouterRetryTest(unittest.TestCase):
    def test_rate_limit_is_retried_then_succeeds(self) -> None:
        rate_limited = Mock(status_code=429, text="slow down")
        succeeded = _response({"title": "Issue", "bullets": ["One.", "Two."]})

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
            "newsletter_summary.openrouter.time.sleep"
        ) as sleep, patch(
            "newsletter_summary.openrouter.requests.post", side_effect=[rate_limited, succeeded]
        ) as post:
            item = summarize_message(
                api_key_env="OPENROUTER_API_KEY",
                keychain_service=None,
                keychain_account=None,
                model="test-model",
                max_input_chars=1000,
                app_name="Newsletter Summary",
                site_url="http://localhost",
                base_prompt="Summarize newsletters.",
                message=_message(),
                body_markdown="Body.",
                links=[],
            )

        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()
        assert item is not None
        self.assertEqual(item.title, "Issue")

    def test_client_errors_are_not_retried(self) -> None:
        with patch("newsletter_summary.openrouter.requests.post", return_value=Mock(status_code=400, text="bad")) as post:
            with self.assertRaises(RuntimeError), patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
                summarize_message(
                    api_key_env="OPENROUTER_API_KEY",
                    keychain_service=None,
                    keychain_account=None,
                    model="test-model",
                    max_input_chars=1000,
                    app_name="Newsletter Summary",
                    site_url="http://localhost",
                    base_prompt="Summarize newsletters.",
                    message=_message(),
                    body_markdown="Body.",
                    links=[],
                )

        self.assertEqual(post.call_count, 1)


class DigestTest(unittest.TestCase):
    def test_window_runs_from_7pm_prior_day_to_7pm(self) -> None:
        start, end = digest_window(date(2026, 10, 14), EASTERN, 19)
        self.assertEqual(start.isoformat(), "2026-10-13T19:00:00-04:00")
        self.assertEqual(end.isoformat(), "2026-10-14T19:00:00-04:00")

    def test_newsletters_are_grouped_by_window_and_open_windows_are_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "items.sqlite3") as store:
            for gmail_id, received in [
                ("before-cutoff", datetime(2026, 10, 13, 18, 59, tzinfo=EASTERN)),
                ("at-cutoff", datetime(2026, 10, 13, 19, 0, tzinfo=EASTERN)),
                ("next-morning", datetime(2026, 10, 14, 8, 0, tzinfo=EASTERN)),
                ("after-cutoff", datetime(2026, 10, 14, 19, 30, tzinfo=EASTERN)),
            ]:
                store.add_summary(_message(gmail_id, received), _summary(gmail_id))
            summaries = store.list_summaries()

        digests = build_digests(
            summaries, timezone=EASTERN, hour=19, now=datetime(2026, 10, 14, 19, 1, tzinfo=EASTERN)
        )

        self.assertEqual([digest.day for digest in digests], [date(2026, 10, 14), date(2026, 10, 13)])
        self.assertEqual([item.title for item in digests[0].newsletters], ["at-cutoff", "next-morning"])
        self.assertEqual([item.title for item in digests[1].newsletters], ["before-cutoff"])
        # Before 7 PM, today's digest is still collecting and is not published.
        earlier = build_digests(
            summaries, timezone=EASTERN, hour=19, now=datetime(2026, 10, 14, 18, 59, tzinfo=EASTERN)
        )
        self.assertEqual([digest.day for digest in earlier], [date(2026, 10, 13)])

    def test_feed_has_one_entry_per_day_listing_each_newsletter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Store(root / "items.sqlite3") as store:
                store.add_summary(
                    _message("one", datetime(2026, 10, 14, 8, 0, tzinfo=EASTERN)),
                    _summary("Big <news>", content_access="partial"),
                )
                store.add_summary(
                    _message("two", datetime(2026, 10, 14, 9, 0, tzinfo=EASTERN)),
                    _summary("Second story", authors=[], url="https://example.com/second"),
                )
                summaries = store.list_summaries()
            feed = ET.ElementTree(
                ET.fromstring(
                    render_digest_feed(
                        build_digests(summaries, timezone=EASTERN, hour=19, now=datetime(2026, 10, 15, tzinfo=EASTERN)),
                        title="Newsletter Highlights",
                        description="Daily digest.",
                        self_url="https://example.com/digest/token/feed.xml",
                    )
                )
            )

        # Its own home page and icon, distinct from the full-text feed's.
        self.assertEqual(feed.findtext("./channel/link"), "https://example.com/newsletters/digest/")
        self.assertEqual(feed.findtext("./channel/image/url"), "https://example.com/newsletters/icons/digest.png")
        items = feed.findall("./channel/item")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.findtext("title"), "Newsletters for Wednesday, October 14, 2026 (2 newsletters)")
        self.assertEqual(item.findtext("guid"), "newsletter-digest:2026-10-14")
        self.assertEqual(item.findtext("pubDate"), "Wed, 14 Oct 2026 19:00:00 -0400")
        description = item.findtext("description") or ""
        self.assertIn('<a href="https://example.com/p/big-news">Big &lt;news&gt;</a>', description)
        self.assertIn("<strong>Daily Dispatch</strong> · by Ada Writer, Bo Editor · <em>partial preview</em>", description)
        self.assertIn('<a href="https://example.com/second">Second story</a>', description)
        self.assertIn("<ul><li>First key point.</li><li>Second key point.</li></ul>", description)
        self.assertLess(description.index("Big &lt;news&gt;"), description.index("Second story"))
        self.assertNotIn("worth reading", description)

    def test_feed_has_a_self_link(self) -> None:
        feed_xml = render_digest_feed(
            [], title="Newsletter Highlights", description="Daily highlights.",
            self_url="https://example.com/digest/token/feed.xml",
        ).decode("utf-8")
        self.assertIn('<atom:link href="https://example.com/digest/token/feed.xml" rel="self"', feed_xml)
        self.assertIn('xmlns:atom="http://www.w3.org/2005/Atom"', feed_xml)


class ReadInFullLinkTest(unittest.TestCase):
    def _description(self, web_public_url: str, gmail_id: str = "18c0ffee00000001") -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Store(root / "items.sqlite3") as store:
                store.add_summary(_message(gmail_id, datetime(2026, 10, 14, 8, 0, tzinfo=EASTERN)), _summary())
                summaries = store.list_summaries()
            feed = render_digest_feed(
                build_digests(summaries, timezone=EASTERN, hour=19, now=datetime(2026, 10, 15, tzinfo=EASTERN)),
                title="Newsletter Highlights",
                description="Daily digest.",
                web_public_url=web_public_url,
            )
            return ET.fromstring(feed).findtext("./channel/item/description") or ""

    def test_digest_links_to_read_in_full_when_public_url_is_set(self) -> None:
        description = self._description("https://newsletters.example.com/")
        self.assertIn(
            '<a href="https://newsletters.example.com/read/18c0ffee00000001">Read in full</a>', description
        )

    def test_digest_has_no_read_in_full_link_without_public_url(self) -> None:
        self.assertNotIn("Read in full", self._description(""))
        self.assertNotIn("/read/", self._description(""))

    def test_non_gmail_ids_get_no_link(self) -> None:
        self.assertNotIn("Read in full", self._description("https://newsletters.example.com", gmail_id="legacy-id"))


class StoreMigrationTest(unittest.TestCase):
    def test_version_two_database_gains_full_text_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "items.sqlite3"
            with Store(path) as store:
                store.add_summary(_message("18c0ffee00000001"), _summary())
            with sqlite3.connect(path) as connection:
                connection.execute("drop table full_items")
                connection.execute("drop table message_log")
                connection.execute("pragma user_version = 2")
            connection.close()

            with Store(path) as store:
                tables = {
                    row[0] for row in store.connection.execute("select name from sqlite_master where type = 'table'")
                }
                self.assertTrue({"full_items", "message_log", "email_content"} <= tables)
                version = store.connection.execute("pragma user_version").fetchone()[0]
                self.assertEqual(version, 6)
                self.assertTrue(store.is_known_gmail_id("18c0ffee00000001"))
                self.assertFalse(store.is_known_gmail_id("18c0ffee00000002"))
                store.log_message("18c0ffee00000002", status="fetch_failed", detail="Gmail 500")
                store.log_message(
                    "18c0ffee00000002", status="summarized", subject="Later", received_at="2026-06-12T00:00:00+00:00"
                )
                store.log_message("18c0ffee00000002", status="summary_failed")
                self.assertTrue(store.is_known_gmail_id("18c0ffee00000002"))
                messages = store.list_messages(10)
                summary = store.get_summary_by_gmail_id("18c0ffee00000001")

        self.assertEqual([row["gmail_id"] for row in messages], ["18c0ffee00000002", "18c0ffee00000001"])
        # Later writes keep fields they leave blank.
        self.assertEqual(messages[0]["subject"], "Later")
        self.assertEqual(messages[0]["status"], "summary_failed")
        # Summaries from before message_log are listed as summarized history.
        self.assertEqual(messages[1]["status"], "summarized")
        assert summary is not None
        self.assertEqual(summary.bullets, ["First key point.", "Second key point."])

    def test_full_items_upsert_list_and_delete(self) -> None:
        from newsletter_summary.store import FullItem

        def item(gmail_id: str, added_at: str, title: str = "Title") -> FullItem:
            return FullItem(
                gmail_id=gmail_id, title=title, url="", publication="Daily", authors=["A", "B"],
                content_access="full", source_gmail_url="", bullets=["One"], content_html="<p>x</p>",
                email_received_at="2026-06-11T09:00:00+00:00", added_at=added_at,
            )

        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "items.sqlite3") as store:
            store.add_full_item(item("aaaaaaaa", "2026-06-11T10:00:00+00:00"))
            store.add_full_item(item("bbbbbbbb", "2026-06-12T10:00:00+00:00"))
            store.add_full_item(item("aaaaaaaa", "2026-06-13T10:00:00+00:00", title="Updated"))
            listed = store.list_full_items(10)
            self.assertEqual([entry.gmail_id for entry in listed], ["bbbbbbbb", "aaaaaaaa"])
            updated = store.get_full_item("aaaaaaaa")
            assert updated is not None
            self.assertEqual(updated.title, "Updated")
            self.assertEqual(updated.added_at, "2026-06-11T10:00:00+00:00")
            self.assertEqual(updated.authors, ["A", "B"])
            self.assertEqual(len(store.list_full_items(1)), 1)
            self.assertTrue(store.delete_full_item("aaaaaaaa"))
            self.assertFalse(store.delete_full_item("aaaaaaaa"))
            self.assertEqual(store.full_item_ids(), {"bbbbbbbb"})

    def test_missing_full_text_tables_do_not_break_the_store(self) -> None:
        from newsletter_summary.store import StoreFeatureUnavailable

        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "items.sqlite3") as store:
            store.add_summary(_message("18c0ffee00000001"), _summary())
            # As on PostgreSQL before deploy/sql/2026-11-full-text.sql.
            store.connection.execute("drop table full_items")
            store.connection.execute("drop table message_log")
            with self.assertRaises(StoreFeatureUnavailable):
                store.list_full_items(5)
            with self.assertRaises(StoreFeatureUnavailable):
                store.log_message("18c0ffee00000002", status="summarized")
            self.assertTrue(store.is_known_gmail_id("18c0ffee00000001"))
            self.assertFalse(store.is_known_gmail_id("18c0ffee00000002"))
            with self.assertLogs(level="WARNING"):
                self.assertEqual([row["gmail_id"] for row in store.list_messages(5)], ["18c0ffee00000001"])
            self.assertEqual(len(store.list_summaries()), 1)

    def test_version_one_rows_are_migrated_and_collapsed_per_newsletter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "items.sqlite3"
            message = _message()
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    create table feed_items (
                      id text primary key, title text not null, url text not null,
                      recommendation text not null, content_access text not null default 'full',
                      short_summary text not null default '', why_this_matters text not null,
                      novel_points text not null, highlights text not null,
                      source_newsletter text not null, source_gmail_id text not null default '',
                      source_subject text not null, source_sender text not null,
                      source_gmail_url text not null, published_at text not null,
                      email_received_at text not null
                    )
                    """
                )
                for index in range(2):
                    connection.execute(
                        "insert into feed_items values (?, ?, ?, ?, 'full', ?, '', '', '', 'Daily', ?, ?, ?, ?, ?, ?)",
                        (
                            f"old-link-{index}",
                            f"Article {index + 1}",
                            f"https://example.com/{index + 1}",
                            "worth reading",
                            f"Article {index + 1} summary.",
                            message.gmail_id,
                            message.subject,
                            message.sender,
                            message.gmail_url,
                            datetime(2026, 6, 11, 10, index, tzinfo=UTC).isoformat(),
                            message.received_at.isoformat(),
                        ),
                    )
                connection.execute("pragma user_version = 1")
            connection.close()

            with Store(path) as store:
                summaries = store.list_summaries()
                store.add_summary(_message("new", datetime(2026, 6, 12, tzinfo=UTC)), _summary())
                self.assertEqual(store.list_summaries()[-1].authors, ["Ada Writer", "Bo Editor"])
                self.assertEqual(store.list_summaries()[-1].bullets, ["First key point.", "Second key point."])

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].title, message.subject)
        # Stored #inbox/ links are read back as #all/, which also opens label-only mail.
        self.assertEqual(summaries[0].url, "https://mail.google.com/mail/u/0/#all/gmail-123")
        self.assertEqual(summaries[0].summary, "Article 1 summary.")
        self.assertEqual(summaries[0].bullets, [])
        self.assertEqual(summaries[0].authors, [])


class PromptTruncationTest(unittest.TestCase):
    def test_long_body_is_cut_with_an_explicit_marker(self) -> None:
        from newsletter_summary.openrouter import TRUNCATION_MARKER, truncate_body

        self.assertEqual(truncate_body("short", 100), "short")
        cut = truncate_body("x" * 150, 100)
        self.assertEqual(cut, "x" * 100 + TRUNCATION_MARKER)
        _, post = _summarize(_response({"bullets": ["A."]}))
        self.assertNotIn("[Truncated", post.call_args.kwargs["json"]["messages"][1]["content"])

    def test_default_cap_is_about_200k_characters(self) -> None:
        from newsletter_summary.config import load_config

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text('gmail:\n  label: "Newsletters"\n', encoding="utf-8")
            self.assertEqual(load_config(path).summary.max_input_chars, 200_000)


class ModelUrlTest(unittest.TestCase):
    def test_raw_tracking_url_from_the_model_matches_its_normalized_link(self) -> None:
        raw = "https://click.example.net/r?url=https%3A%2F%2Fexample.com%2Fstory%3Futm_source%3Demail"
        item, _ = _summarize(
            _response({"title": "Big news", "url": raw, "bullets": ["A point."]}),
            links=[ExtractedLink(url="https://example.com/story", text="Story")],
        )
        assert item is not None
        self.assertEqual(item.url, "https://example.com/story")

    def test_rejected_url_is_logged_without_its_query_and_falls_back(self) -> None:
        with self.assertLogs(level="INFO") as logs:
            item, _ = _summarize(
                _response({"url": "https://invented.example/article?token=secret123", "bullets": ["A point."]}),
                links=[ExtractedLink(url="https://example.com/story", text="Story")],
                fallback_url="https://newsletters.example.com/read/gmail-123",
            )
        assert item is not None
        self.assertEqual(item.url, "https://newsletters.example.com/read/gmail-123")
        rejected = [line for line in logs.output if "invented.example/article" in line]
        self.assertEqual(len(rejected), 1)
        self.assertIn("INFO", rejected[0])
        self.assertNotIn("secret123", "".join(logs.output))

    def test_fetched_messages_link_to_all_mail_and_keep_metadata_without_a_body(self) -> None:
        from newsletter_summary.gmail_client import fetch_message

        service = Mock()
        service.users().messages().get().execute.return_value = {
            "id": "18c0ffee00000001",
            "internalDate": "1792000000000",
            "payload": {"headers": [{"name": "Subject", "value": "Hi"}, {"name": "From", "value": "A <a@b.c>"}]},
        }
        message = fetch_message(service, "me", "18c0ffee00000001", EASTERN)
        self.assertEqual(message.gmail_url, "https://mail.google.com/mail/u/0/#all/18c0ffee00000001")
        self.assertFalse(message.has_body)
        self.assertEqual(message.subject, "Hi")


class OpenRouterErrorBodyTest(unittest.TestCase):
    def test_error_in_a_200_response_is_retried(self) -> None:
        error = Mock(status_code=200)
        error.json.return_value = {"error": {"code": 502, "message": "Provider returned error"}}
        succeeded = _response({"title": "Issue", "bullets": ["One."]})
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
            "newsletter_summary.openrouter.time.sleep"
        ), patch("newsletter_summary.openrouter.requests.post", side_effect=[error, succeeded]) as post:
            item = summarize_message(
                api_key_env="OPENROUTER_API_KEY", keychain_service=None, keychain_account=None, model="m",
                max_input_chars=1000, app_name="a", site_url="", base_prompt="p", message=_message(),
                body_markdown="Body.", links=[],
            )
        self.assertEqual(post.call_count, 2)
        assert item is not None
        self.assertEqual(item.title, "Issue")

    def test_non_retryable_error_body_is_reported(self) -> None:
        error = Mock(status_code=200)
        error.json.return_value = {"error": {"code": 400, "message": "Context too long"}}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
            "newsletter_summary.openrouter.requests.post", return_value=error
        ) as post, self.assertRaisesRegex(RuntimeError, "Context too long"):
            summarize_message(
                api_key_env="OPENROUTER_API_KEY", keychain_service=None, keychain_account=None, model="m",
                max_input_chars=1000, app_name="a", site_url="", base_prompt="p", message=_message(),
                body_markdown="Body.", links=[],
            )
        self.assertEqual(post.call_count, 1)


class StoreCorrectnessTest(unittest.TestCase):
    def test_duplicate_insert_is_ignored_instead_of_failing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "items.sqlite3"
            with Store(path) as first, Store(path) as second:
                self.assertTrue(first.add_summary(_message("dup"), _summary()))
                # As in an overlapping run that checked before the first insert.
                self.assertFalse(second.add_summary(_message("dup"), _summary("Other")))
                self.assertEqual(len(second.list_summaries()), 1)

    def test_legacy_rows_show_their_paragraph_not_retired_highlights(self) -> None:
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "items.sqlite3") as store:
            store.add_summary(_message("legacy"), _summary())
            store.connection.execute(
                "update feed_items set short_summary = 'The paragraph.', highlights = 'Old line one\nOld line two'"
            )
            summary = store.get_summary_by_gmail_id("legacy")
        assert summary is not None
        self.assertEqual(summary.bullets, [])
        self.assertEqual(summary.summary, "The paragraph.")

    def test_only_missing_tables_count_as_unmigrated(self) -> None:
        from newsletter_summary.store import StoreFeatureUnavailable, is_missing_schema_error

        self.assertTrue(is_missing_schema_error(sqlite3.OperationalError("no such table: message_log")))
        self.assertTrue(is_missing_schema_error(sqlite3.OperationalError("table message_log has no column named attempts")))
        self.assertFalse(is_missing_schema_error(sqlite3.OperationalError("database is locked")))
        self.assertTrue(is_missing_schema_error(type("UndefinedColumn", (Exception,), {"sqlstate": "42703"})()))
        self.assertFalse(is_missing_schema_error(type("QueryCanceled", (Exception,), {"sqlstate": "57014"})()))

        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "items.sqlite3") as store:
            real = store.connection
            broken = Mock(wraps=real)
            broken.execute.side_effect = lambda sql, *args: (
                (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
                if "message_log" in sql
                else real.execute(sql, *args)
            )
            store.connection = broken
            # A timeout must surface as an error, not as "unknown email" (a 404).
            with self.assertRaises(sqlite3.OperationalError):
                store.is_known_gmail_id("18c0ffee00000001")
            store.connection = real
            real.execute("drop table message_log")
            self.assertFalse(store.is_known_gmail_id("18c0ffee00000001"))
            with self.assertRaises(StoreFeatureUnavailable):
                store.gave_up_ids(3)

    def test_attempts_are_counted_and_missing_column_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "items.sqlite3") as store:
            for _ in range(3):
                store.log_message("aaaa1111", status="summary_failed", count_attempt=True)
            store.log_message("bbbb2222", status="summary_failed", count_attempt=True)
            store.log_message("aaaa1111", status="summary_failed", detail="later, uncounted")
            self.assertEqual(store.gave_up_ids(3), {"aaaa1111"})

        # As on PostgreSQL with message_log but before the attempts migration.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "items.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "create table message_log (gmail_id text primary key, subject text not null default '', "
                    "sender text not null default '', gmail_url text not null default '', "
                    "received_at text not null default '', status text not null, detail text not null default '', "
                    "first_seen_at text not null, updated_at text not null)"
                )
                connection.execute("pragma user_version = 99")
            connection.close()
            with Store(path) as store:
                store.connection.execute(
                    "create table if not exists feed_items (id text primary key, source_gmail_id text)"
                )
                with self.assertLogs(level="WARNING"):
                    store.log_message("aaaa1111", status="summary_failed", subject="S", count_attempt=True)
                store.log_message("aaaa1111", status="summary_failed", count_attempt=True)
                rows = store.connection.execute("select subject, status from message_log").fetchall()
                self.assertEqual([tuple(row) for row in rows], [("S", "summary_failed")])
                from newsletter_summary.store import StoreFeatureUnavailable

                with self.assertRaises(StoreFeatureUnavailable):
                    store.gave_up_ids(3)


class PostgresAdapterTest(unittest.TestCase):
    def test_bind_sql_translates_placeholders_and_escapes_percent(self) -> None:
        from newsletter_summary._postgres import bind_sql

        self.assertEqual(bind_sql("select * from t where a = ? and b = ?"), "select * from t where a = %s and b = %s")
        self.assertEqual(bind_sql("select '?' as q, ? as v"), "select '?' as q, %s as v")
        self.assertEqual(bind_sql("select 'it''s ?' , ?"), "select 'it''s ?' , %s")
        self.assertEqual(bind_sql('select "col?" from t where x like \'50%\' and y = ?'),
                         'select "col?" from t where x like \'50%%\' and y = %s')
        self.assertEqual(bind_sql("select 100 % 7, ?"), "select 100 %% 7, %s")
        self.assertEqual(bind_sql("insert into t values (?, ?) on conflict(id) do nothing"),
                         "insert into t values (%s, %s) on conflict(id) do nothing")

    def test_record_supports_names_and_positions(self) -> None:
        from newsletter_summary._postgres import Record, row_factory

        cursor = Mock(description=[Mock(), Mock()])
        cursor.description[0].name = "gmail_id"
        cursor.description[1].name = "status"
        record = row_factory(cursor)(("abc", "summarized"))
        self.assertIsInstance(record, Record)
        self.assertEqual(record["status"], "summarized")
        self.assertEqual(record[0], "abc")
        self.assertEqual(dict(record), {"gmail_id": "abc", "status": "summarized"})

    def test_connection_binds_booleans_as_integers(self) -> None:
        from newsletter_summary._postgres import Connection

        raw = Mock()
        Connection(raw).execute("update t set flag = ? where id = ?", (True, "x"))
        raw.execute.assert_called_once_with("update t set flag = %s where id = %s", (1, "x"))


if __name__ == "__main__":
    unittest.main()
