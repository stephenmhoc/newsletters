from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from newsletter_summary import cli
from newsletter_summary.config import (
    AppConfig,
    GmailConfig,
    RssConfig,
    ServerConfig,
    StorageConfig,
    SummaryConfig,
    WebConfig,
)
from newsletter_summary.digest import PUBLICATION_KEY, load_digests
from newsletter_summary.email_extract import ExtractedLink
from newsletter_summary.gmail_client import NewsletterMessage
from newsletter_summary.openrouter import SummaryItem
from newsletter_summary.store import Store, run_lock


def _config(root: Path) -> AppConfig:
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
            prompt="Summarize.",
        ),
        rss=RssConfig(
            title="Newsletter Highlights",
            description="Daily highlights.",
            public_url="https://example.com/feed.xml",
            max_items=None,
        ),
        server=ServerConfig(host="127.0.0.1", port=0),
        storage=StorageConfig(sqlite_path=root / "items.sqlite3", log_path=root / "log.txt"),
    )


def _message(gmail_id: str, subject: str) -> NewsletterMessage:
    return NewsletterMessage(
        gmail_id=gmail_id,
        thread_id=f"thread-{gmail_id}",
        subject=subject,
        sender="editor@example.com",
        received_at=datetime.fromisoformat("2026-06-28T09:00:00-04:00"),
        html="<a href='https://example.com/story'>Story</a>",
        text="Story https://example.com/story",
        gmail_url=f"https://mail.google.com/mail/u/0/#all/{gmail_id}",
    )


def _digests(config: AppConfig) -> list:
    with Store(config.storage.sqlite_path) as store:
        return load_digests(store, config, now=datetime.now(config.gmail.tzinfo))


def _gmail(ids: list[str], fetch: object):
    """Patch Gmail: these candidate IDs, fetched by `fetch` (side_effect or return value)."""
    fetch_patch = (
        patch("newsletter_summary.cli.fetch_message", side_effect=fetch)
        if isinstance(fetch, list) or callable(fetch)
        else patch("newsletter_summary.cli.fetch_message", return_value=fetch)
    )
    return (
        patch("newsletter_summary.cli.build_gmail_service", return_value=Mock()),
        patch("newsletter_summary.cli.find_label_id", return_value="Label_123"),
        patch("newsletter_summary.cli.list_messages_for_window", return_value=ids),
        fetch_patch,
    )


def _summary(url: str = "https://example.com/story") -> SummaryItem:
    return SummaryItem(
        title="Story",
        url=url,
        publication="Daily",
        authors=["Ada Writer"],
        bullets=["One point.", "Another point."],
        content_access="full",
    )


class RunCommandTests(unittest.TestCase):
    def test_default_run_uses_latest_28_hours(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            with patch("newsletter_summary.cli.build_gmail_service", return_value=Mock()), patch(
                "newsletter_summary.cli.find_label_id", return_value="Label_123"
            ), patch("newsletter_summary.cli.list_messages_for_window", return_value=[]) as list_messages:
                self.assertEqual(cli.run(config, target=None, dry_run=True), 0)

        _, _, _, start, end = list_messages.call_args.args
        self.assertEqual(end - start, cli.timedelta(hours=28))

    def test_date_backfill_uses_the_digest_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            with patch("newsletter_summary.cli.build_gmail_service", return_value=Mock()), patch(
                "newsletter_summary.cli.find_label_id", return_value="Label_123"
            ), patch("newsletter_summary.cli.list_messages_for_window", return_value=[]) as list_messages:
                self.assertEqual(cli.run(config, target="2026-06-28", dry_run=True), 0)

        _, _, _, start, end = list_messages.call_args.args
        self.assertEqual(start.isoformat(), "2026-06-27T19:00:00-04:00")
        self.assertEqual(end.isoformat(), "2026-06-28T19:00:00-04:00")

    def test_run_skips_duplicate_gmail_messages_before_summarizing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            old_message = _message("old-gmail-id", "Already summarized")
            new_message = _message("new-gmail-id", "Needs summary")
            with Store(config.storage.sqlite_path) as store:
                self.assertTrue(store.add_summary(old_message, _summary(old_message.gmail_url)))

            with patch("newsletter_summary.cli.build_gmail_service", return_value=Mock()), patch(
                "newsletter_summary.cli.find_label_id", return_value="Label_123"
            ), patch(
                "newsletter_summary.cli.list_messages_for_window", return_value=["old-gmail-id", "new-gmail-id"]
            ), patch(
                "newsletter_summary.cli.fetch_message", return_value=new_message
            ) as fetch, patch(
                "newsletter_summary.cli.extract_links",
                return_value=[ExtractedLink(url="https://example.com/story", text="Story")],
            ), patch(
                "newsletter_summary.cli.email_to_markdown", return_value="Story"
            ), patch(
                "newsletter_summary.cli.summarize_message", return_value=_summary()
            ) as summarize:
                self.assertEqual(cli.run(config, target="2026-06-28", dry_run=False), 0)

            # The already-summarized email is not even downloaded again.
            self.assertEqual([call.args[2] for call in fetch.call_args_list], ["new-gmail-id"])
            summarize.assert_called_once()
            self.assertEqual(summarize.call_args.kwargs["message"].gmail_id, "new-gmail-id")
            with Store(config.storage.sqlite_path) as store:
                self.assertTrue(store.message_has_items("old-gmail-id"))
                self.assertTrue(store.message_has_items("new-gmail-id"))

    def test_one_failed_summary_does_not_abort_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            broken = _message("broken-gmail-id", "Breaks the summarizer")
            healthy = _message("healthy-gmail-id", "Summarizes fine")

            with patch("newsletter_summary.cli.build_gmail_service", return_value=Mock()), patch(
                "newsletter_summary.cli.find_label_id", return_value="Label_123"
            ), patch(
                "newsletter_summary.cli.list_messages_for_window",
                return_value=["broken-gmail-id", "healthy-gmail-id"],
            ), patch(
                "newsletter_summary.cli.fetch_message", side_effect=[broken, healthy]
            ), patch(
                "newsletter_summary.cli.extract_links",
                return_value=[ExtractedLink(url="https://example.com/story", text="Story")],
            ), patch(
                "newsletter_summary.cli.email_to_markdown", return_value="Story"
            ), patch(
                "newsletter_summary.cli.summarize_message",
                side_effect=[RuntimeError("OpenRouter error 429"), _summary()],
            ):
                self.assertEqual(cli.run(config, target="2026-06-28", dry_run=False), 1)

            # The healthy message is still summarized and the digest is still published,
            # listing the failure under "Not summarized".
            with Store(config.storage.sqlite_path) as store:
                self.assertFalse(store.message_has_items("broken-gmail-id"))
                self.assertTrue(store.message_has_items("healthy-gmail-id"))
            digests = _digests(config)
            self.assertEqual(digests[0].id, "newsletter-digest:2026-06-28")
            self.assertEqual([item.source_gmail_id for item in digests[0].newsletters], ["healthy-gmail-id"])
            self.assertEqual([item.gmail_id for item in digests[0].not_summarized], ["broken-gmail-id"])

    def test_unreadable_gmail_message_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            healthy = _message("healthy-gmail-id", "Summarizes fine")

            with patch("newsletter_summary.cli.build_gmail_service", return_value=Mock()), patch(
                "newsletter_summary.cli.find_label_id", return_value="Label_123"
            ), patch(
                "newsletter_summary.cli.list_messages_for_window",
                return_value=["broken-gmail-id", "healthy-gmail-id"],
            ), patch(
                "newsletter_summary.cli.fetch_message",
                side_effect=[RuntimeError("Gmail 500"), healthy],
            ):
                self.assertEqual(cli.run(config, target="2026-06-28", dry_run=True), 1)

    def test_unreadable_message_fails_real_run_but_still_publishes_healthy_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            with patch("newsletter_summary.cli.build_gmail_service", return_value=Mock()), patch(
                "newsletter_summary.cli.find_label_id", return_value="Label_123"
            ), patch(
                "newsletter_summary.cli.list_messages_for_window", return_value=["broken", "healthy"]
            ), patch(
                "newsletter_summary.cli.fetch_message", side_effect=[RuntimeError("Gmail 500"), _message("healthy", "Healthy")]
            ), patch("newsletter_summary.cli.summarize_message", return_value=_summary()):
                self.assertEqual(cli.run(config, target="2026-06-28", dry_run=False), 1)
            with Store(config.storage.sqlite_path) as store:
                self.assertTrue(store.message_has_items("healthy"))
                self.assertTrue(store.get_setting(PUBLICATION_KEY))
            self.assertEqual(len(_digests(config)[0].newsletters), 1)

    def test_empty_summary_is_reported_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            with patch("newsletter_summary.cli.build_gmail_service", return_value=Mock()), patch(
                "newsletter_summary.cli.find_label_id", return_value="Label_123"
            ), patch("newsletter_summary.cli.list_messages_for_window", return_value=["empty"]), patch(
                "newsletter_summary.cli.fetch_message", return_value=_message("empty", "Empty")
            ), patch("newsletter_summary.cli.summarize_message", return_value=None):
                self.assertEqual(cli.run(config, target="2026-06-28", dry_run=False), 1)

    def test_run_logs_every_processed_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            old = _message("old", "Already summarized")
            with Store(config.storage.sqlite_path) as store:
                store.add_summary(old, _summary(old.gmail_url))
            no_links = _message("nolinks", "No links")
            broken = _message("broken", "Summary breaks")
            healthy = _message("healthy", "Summarizes")

            def links(html: str, text: str) -> list[ExtractedLink]:
                return [] if html == "no-links" else [ExtractedLink(url="https://example.com/story", text="Story")]

            no_links = dataclasses.replace(no_links, html="no-links")
            with patch("newsletter_summary.cli.build_gmail_service", return_value=Mock()), patch(
                "newsletter_summary.cli.find_label_id", return_value="Label_123"
            ), patch(
                "newsletter_summary.cli.list_messages_for_window",
                return_value=["unreadable", "empty", "old", "nolinks", "broken", "healthy"],
            ), patch(
                "newsletter_summary.cli.fetch_message",
                side_effect=[RuntimeError("Gmail 500"), None, no_links, broken, healthy],
            ), patch("newsletter_summary.cli.extract_links", side_effect=links), patch(
                "newsletter_summary.cli.summarize_message", side_effect=[RuntimeError("OpenRouter 429"), _summary()]
            ):
                self.assertEqual(cli.run(config, target="2026-06-28", dry_run=False), 1)

            with Store(config.storage.sqlite_path) as store:
                rows = {row["gmail_id"]: row for row in store.list_messages(20)}
        self.assertEqual(
            {gmail_id: row["status"] for gmail_id, row in rows.items()},
            {
                "unreadable": "fetch_failed",
                "empty": "skipped_no_body",
                "old": "summarized",
                "nolinks": "skipped_no_links",
                "broken": "summary_failed",
                "healthy": "summarized",
            },
        )
        self.assertIn("Gmail 500", rows["unreadable"]["detail"])
        self.assertIn("OpenRouter 429", rows["broken"]["detail"])
        self.assertEqual(rows["healthy"]["subject"], "Summarizes")
        self.assertEqual(rows["healthy"]["received_at"], "2026-06-28T09:00:00-04:00")

    def test_message_log_failure_does_not_fail_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            with patch("newsletter_summary.cli.build_gmail_service", return_value=Mock()), patch(
                "newsletter_summary.cli.find_label_id", return_value="Label_123"
            ), patch("newsletter_summary.cli.list_messages_for_window", return_value=["healthy"]), patch(
                "newsletter_summary.cli.fetch_message", return_value=_message("healthy", "Healthy")
            ), patch("newsletter_summary.cli.summarize_message", return_value=_summary()), patch.object(
                Store, "log_message", side_effect=RuntimeError("relation message_log does not exist")
            ) as log_message, self.assertLogs(level="WARNING") as logs:
                self.assertEqual(cli.run(config, target="2026-06-28", dry_run=False), 0)
            log_message.assert_called_once()
            self.assertTrue(any("message_log" in line for line in logs.output))
            with Store(config.storage.sqlite_path) as store:
                self.assertTrue(store.message_has_items("healthy"))

    def test_summary_is_given_up_after_three_failed_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            broken = dataclasses.replace(
                _message("broken", "Never summarizes"),
                received_at=datetime.now(config.gmail.tzinfo) - cli.timedelta(hours=1),
            )
            results = []
            for _ in range(4):
                patches = _gmail(["broken"], broken)
                with patches[0], patches[1], patches[2], patches[3] as fetch, patch(
                    "newsletter_summary.cli.summarize_message", return_value=None
                ) as summarize:
                    results.append(cli.run(config, target=None, dry_run=False))
                calls = summarize.call_count, fetch.call_count
            # Three failing runs, then the email is left alone and the run succeeds,
            # so the retry window stops being pinned.
            self.assertEqual(results, [1, 1, 1, 0])
            self.assertEqual(calls, (0, 0))
            with Store(config.storage.sqlite_path) as store:
                self.assertEqual(store.gave_up_ids(3), {"broken"})
            self.assertEqual(cli.RunState(config).data["status"], "ok")

    def test_overlapping_run_is_refused_without_touching_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            with run_lock(config.storage.sqlite_path), patch("newsletter_summary.cli.build_gmail_service") as gmail:
                self.assertEqual(cli.run(config, target=None, dry_run=False), 1)
            gmail.assert_not_called()
            self.assertEqual(cli.RunState(config).data, {})
            # Released afterwards.
            patches = _gmail([], None)
            with patches[0], patches[1], patches[2], patches[3]:
                self.assertEqual(cli.run(config, target=None, dry_run=False), 0)

    def test_fallback_link_is_the_reading_view_when_the_site_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for web, expected in (
                (WebConfig(), "https://mail.google.com/mail/u/0/#all/18c0ffee00000001"),
                (
                    WebConfig(enabled=True, public_url="https://newsletters.example.com"),
                    "https://newsletters.example.com/item/18c0ffee00000001",
                ),
            ):
                config = dataclasses.replace(_config(Path(directory)), web=web)
                patches = _gmail(["18c0ffee00000001"], _message("18c0ffee00000001", "Issue"))
                with patches[0], patches[1], patches[2], patches[3], patch(
                    "newsletter_summary.cli.summarize_message", return_value=None
                ) as summarize:
                    cli.run(config, target="2026-06-28", dry_run=False)
                self.assertEqual(summarize.call_args.kwargs["fallback_url"], expected)

    def test_doctor_fails_for_missing_key_or_gmail_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            with patch("newsletter_summary.cli.get_secret", return_value=""), patch(
                "newsletter_summary.cli.build_gmail_service", return_value=Mock()
            ), patch("newsletter_summary.cli.find_label_id", return_value="Label_123"):
                self.assertEqual(cli.doctor(config), 1)
            with patch("newsletter_summary.cli.get_secret", return_value="test-key"), patch(
                "newsletter_summary.cli.build_gmail_service", side_effect=RuntimeError("Expired auth")
            ):
                self.assertEqual(cli.doctor(config), 1)


if __name__ == "__main__":
    unittest.main()
