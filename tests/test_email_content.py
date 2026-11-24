from __future__ import annotations

import dataclasses
import io
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from test_cli_run import _config, _gmail, _summary

from newsletter_summary import cli, email_content
from newsletter_summary.digest import load_digests, publish
from newsletter_summary.email_content import (
    current_content,
    resanitize_all,
    save_message,
    with_stored_content,
)
from newsletter_summary.full_content import SANITIZER_VERSION
from newsletter_summary.gmail_client import NewsletterMessage
from newsletter_summary.openrouter import SummaryItem
from newsletter_summary.store import FullItem, Store, StoreFeatureUnavailable

HTML = """<html><body><h1>Headline</h1><p>One two three four five.</p>
<img src="https://cdn.example.com/a.png" width="600"><script>alert(1)</script>
<p><a href="https://example.com/story">Story</a></p></body></html>"""


def _message(gmail_id: str, subject: str = "Issue", received: str = "2026-06-28T09:00:00-04:00") -> NewsletterMessage:
    return NewsletterMessage(
        gmail_id=gmail_id,
        thread_id=f"thread-{gmail_id}",
        subject=subject,
        sender="Daily <editor@example.com>",
        received_at=datetime.fromisoformat(received),
        html=HTML,
        text="",
        gmail_url=f"https://mail.google.com/mail/u/0/#all/{gmail_id}",
    )


class ContentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "items.sqlite3")
        self.addCleanup(self.store.close)

    def test_saved_content_keeps_the_raw_email_and_a_sanitized_copy(self) -> None:
        saved = save_message(self.store, _message("aaaa0001"))
        stored = self.store.get_content("aaaa0001")
        assert stored is not None
        self.assertEqual(stored.raw_html, HTML)
        self.assertNotIn("<script", stored.content_html)
        self.assertIn("One two three", stored.content_html)
        self.assertEqual(stored.sanitizer_version, SANITIZER_VERSION)
        self.assertEqual(stored.word_count, saved.word_count)
        self.assertEqual(stored.word_count, 7)  # Headline One two three four five. Story
        self.assertEqual(stored.received_at, "2026-06-28T09:00:00-04:00")

    def test_older_sanitizer_output_is_rederived_on_read_and_saved(self) -> None:
        save_message(self.store, _message("aaaa0001"))
        self.store.update_sanitized("aaaa0001", content_html="<p>old</p>", sanitizer_version=0, word_count=1)
        fresh = current_content(self.store, "aaaa0001")
        assert fresh is not None
        self.assertIn("One two three", fresh.content_html)
        stored = self.store.get_content("aaaa0001")
        assert stored is not None
        self.assertEqual(stored.sanitizer_version, SANITIZER_VERSION)
        self.assertEqual(stored.content_html, fresh.content_html)

    def test_resanitize_rewrites_stale_rows_only_unless_forced(self) -> None:
        save_message(self.store, _message("aaaa0001"))
        save_message(self.store, _message("aaaa0002"))
        self.store.update_sanitized("aaaa0001", content_html="<p>old</p>", sanitizer_version=0, word_count=1)
        with patch.object(email_content, "SANITIZER_VERSION", SANITIZER_VERSION):
            self.assertEqual(resanitize_all(self.store), (1, 0))
            self.assertEqual(resanitize_all(self.store), (0, 0))
            self.assertEqual(resanitize_all(self.store, force=True), (2, 0))
        stored = self.store.get_content("aaaa0001")
        assert stored is not None
        self.assertNotEqual(stored.content_html, "<p>old</p>")

    def test_a_sanitizer_bump_marks_every_row_stale(self) -> None:
        save_message(self.store, _message("aaaa0001"))
        with patch.object(email_content, "SANITIZER_VERSION", SANITIZER_VERSION + 1):
            self.assertEqual(self.store.stale_content_ids(SANITIZER_VERSION + 1), ["aaaa0001"])
            fresh = current_content(self.store, "aaaa0001")
        assert fresh is not None
        self.assertEqual(fresh.sanitizer_version, SANITIZER_VERSION + 1)

    def test_reading_list_items_take_their_content_from_the_store(self) -> None:
        save_message(self.store, _message("aaaa0001"))
        item = FullItem(
            gmail_id="aaaa0001", title="Issue", url="", publication="Daily", authors=[], content_access="full",
            source_gmail_url="", bullets=[], content_html="", email_received_at="2026-06-28T13:00:00+00:00",
            added_at="2026-06-28T14:00:00+00:00",
        )
        legacy = dataclasses.replace(item, gmail_id="aaaa0002", content_html="<p>Legacy copy</p>")
        self.store.add_full_item(item)
        self.store.add_full_item(legacy)
        resolved = {entry.gmail_id: entry for entry in with_stored_content(self.store, self.store.list_full_items(10))}
        self.assertIn("One two three", resolved["aaaa0001"].content_html)
        # Added before content was stored: its own copy is the fallback.
        self.assertEqual(resolved["aaaa0002"].content_html, "<p>Legacy copy</p>")

    def test_without_the_table_reads_degrade_and_writes_raise(self) -> None:
        self.store.add_summary(_message("aaaa0001"), _summary())
        self.store.connection.execute("drop table email_content")
        self.assertIsNone(current_content(self.store, "aaaa0001"))
        with self.assertRaises(StoreFeatureUnavailable):
            save_message(self.store, _message("aaaa0001"))
        # Summaries still load, with no word count.
        self.assertEqual(self.store.list_summaries()[0].word_count, 0)
        state = {feature: installed for feature, _, installed in self.store.migration_state()}
        self.assertEqual(state["Stored email content"], False)
        self.assertEqual(state["Reading list"], True)

    def test_summaries_carry_the_stored_word_count(self) -> None:
        self.store.add_summary(_message("aaaa0001"), _summary())
        save_message(self.store, _message("aaaa0001"))
        self.assertEqual(self.store.list_summaries()[0].word_count, 7)
        summary = self.store.get_summary_by_gmail_id("aaaa0001")
        assert summary is not None
        self.assertEqual(summary.word_count, 7)


class ReplaceSummaryTests(unittest.TestCase):
    def test_replacing_keeps_the_original_publication_time_and_drops_old_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "items.sqlite3") as store:
            message = _message("aaaa0001")
            # A paragraph-era row: short_summary set, retired highlights.
            store.connection.execute(
                """insert into feed_items (id, title, url, recommendation, content_access, short_summary,
                   why_this_matters, novel_points, highlights, source_newsletter, source_gmail_id, source_subject,
                   source_sender, source_gmail_url, published_at, email_received_at, authors)
                   values ('old', 'Old', 'https://example.com/old', '', 'full', 'A paragraph.', '', '', 'x\ny',
                   'Daily', ?, 'Issue', 'editor@example.com', '', '2026-06-28T23:00:05+00:00', ?, '')""",
                (message.gmail_id, message.received_at.isoformat()),
            )
            store.connection.commit()
            before = store.get_summary_by_gmail_id(message.gmail_id)
            assert before is not None
            self.assertEqual(before.bullets, [])
            self.assertTrue(store.replace_summary(message, _summary()))
            after = store.get_summary_by_gmail_id(message.gmail_id)
            assert after is not None
            self.assertEqual(after.bullets, ["One point.", "Another point."])
            self.assertEqual(after.summary, "")
            self.assertEqual(after.created_at, datetime(2026, 6, 28, 23, 0, 5, tzinfo=UTC))
            rows = store.connection.execute(
                "select count(*) from feed_items where source_gmail_id = ?", (message.gmail_id,)
            ).fetchone()[0]
            self.assertEqual(rows, 1)
            # An email with no summary is not given one.
            self.assertFalse(store.replace_summary(_message("aaaa0002"), _summary()))
            self.assertIsNone(store.get_summary_by_gmail_id("aaaa0002"))


class RunStoresContentTests(unittest.TestCase):
    def test_run_stores_every_processed_email_even_when_its_summary_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            messages = {
                "aaaa0001": _message("aaaa0001", "Good"),
                "aaaa0002": _message("aaaa0002", "Breaks"),
                "aaaa0003": dataclasses.replace(_message("aaaa0003", "No links"), html="<p>No links here.</p>"),
            }

            def summarize(**kwargs):
                if kwargs["message"].gmail_id == "aaaa0002":
                    raise RuntimeError("OpenRouter 500")
                return _summary()

            patches = _gmail(list(messages), lambda _service, _user, gmail_id, _tz: messages[gmail_id])
            with patches[0], patches[1], patches[2], patches[3], patch(
                "newsletter_summary.cli.summarize_message", side_effect=summarize
            ):
                cli.run(config, target="2026-06-28", dry_run=False)
            with Store(config.storage.sqlite_path) as store:
                self.assertEqual(sorted(store.content_ids()), ["aaaa0001", "aaaa0002", "aaaa0003"])

    def test_dry_run_stores_nothing_and_a_missing_table_does_not_fail_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            message = _message("aaaa0001")
            patches = _gmail(["aaaa0001"], message)
            with patches[0], patches[1], patches[2], patches[3]:
                cli.run(config, target="2026-06-28", dry_run=True)
            with Store(config.storage.sqlite_path) as store:
                self.assertEqual(store.content_ids(), [])
                store.connection.execute("drop table email_content")
            patches = _gmail(["aaaa0001"], message)
            with patches[0], patches[1], patches[2], patches[3], patch(
                "newsletter_summary.cli.summarize_message", return_value=_summary()
            ), self.assertLogs(level="WARNING") as logs:
                self.assertEqual(cli.run(config, target="2026-06-28", dry_run=False), 0)
            self.assertTrue(any("2026-11-24-email-content.sql" in line for line in logs.output))
            with Store(config.storage.sqlite_path) as store:
                self.assertIsNotNone(store.get_summary_by_gmail_id("aaaa0001"))


class ResanitizeCommandTests(unittest.TestCase):
    def test_command_reports_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            with Store(config.storage.sqlite_path) as store:
                save_message(store, _message("aaaa0001"))
                store.update_sanitized("aaaa0001", content_html="", sanitizer_version=0, word_count=0)
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(cli.resanitize(config), 0)
            self.assertIn("Re-sanitized 1 stored email(s)", out.getvalue())


class ResummarizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = _config(Path(self.directory.name))
        with Store(self.config.storage.sqlite_path) as store:
            # Before the cut-off: never touched.
            store.add_summary(_message("aaaa0000", received="2026-11-10T09:00:00-05:00"), _summary())
            # Stored content, summarized with bullets.
            store.add_summary(_message("aaaa0001", received="2026-11-16T09:00:00-05:00"), _summary())
            save_message(store, _message("aaaa0001", received="2026-11-16T09:00:00-05:00"))
            # No stored content: needs Gmail.
            store.add_summary(_message("aaaa0002", received="2026-11-17T09:00:00-05:00"), _summary())

    def summaries(self) -> dict:
        with Store(self.config.storage.sqlite_path) as store:
            return {summary.source_gmail_id: summary for summary in store.list_summaries()}

    def test_dry_run_prints_the_count_and_estimate_without_calling_anything(self) -> None:
        with patch("newsletter_summary.cli.summarize_message") as summarize, patch(
            "newsletter_summary.cli.build_gmail_service"
        ) as gmail, patch("sys.stdout", new_callable=io.StringIO) as out:
            code = cli.resummarize(self.config, since="2026-11-15", dry_run=True)
        self.assertEqual(code, 0)
        summarize.assert_not_called()
        gmail.assert_not_called()
        self.assertIn("2 email(s) received since 2026-11-15 would be re-summarized", out.getvalue())
        self.assertIn("1 must be fetched from Gmail first", out.getvalue())
        self.assertIn("Estimate: 2 model call(s)", out.getvalue())

    def test_refuses_without_confirmation_when_not_interactive(self) -> None:
        with patch("newsletter_summary.cli.summarize_message") as summarize, patch(
            "sys.stdin", io.StringIO("")
        ), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(cli.resummarize(self.config, since="2026-11-15", dry_run=False), 1)
        summarize.assert_not_called()

    def test_replaces_summaries_using_stored_content_then_gmail(self) -> None:
        new = SummaryItem(
            title="New title", url="https://example.com/story", publication="Daily", authors=[],
            bullets=["Fresh point."], content_access="full",
        )
        before = self.summaries()
        fetched = _message("aaaa0002", received="2026-11-17T09:00:00-05:00")
        with patch("newsletter_summary.cli.summarize_message", return_value=new) as summarize, patch(
            "newsletter_summary.cli.build_gmail_service", return_value=Mock()
        ), patch("newsletter_summary.cli.fetch_message", return_value=fetched) as fetch, patch(
            "sys.stdout", new_callable=io.StringIO
        ) as out:
            code = cli.resummarize(self.config, since="2026-11-15", dry_run=False, assume_yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(summarize.call_count, 2)
        # Only the email without stored content goes to Gmail, and is then stored.
        self.assertEqual([call.args[2] for call in fetch.call_args_list], ["aaaa0002"])
        self.assertIn("Re-summarized 2 email(s); 0 failed.", out.getvalue())
        after = self.summaries()
        self.assertEqual(after["aaaa0000"].title, before["aaaa0000"].title)
        for gmail_id in ("aaaa0001", "aaaa0002"):
            self.assertEqual(after[gmail_id].title, "New title")
            self.assertEqual(after[gmail_id].bullets, ["Fresh point."])
            self.assertEqual(after[gmail_id].created_at, before[gmail_id].created_at)
        with Store(self.config.storage.sqlite_path) as store:
            self.assertIsNotNone(store.get_content("aaaa0002"))

    def test_a_failure_keeps_the_old_summary(self) -> None:
        before = self.summaries()
        with patch("newsletter_summary.cli.summarize_message", side_effect=RuntimeError("OpenRouter 500")), patch(
            "newsletter_summary.cli.build_gmail_service", side_effect=RuntimeError("Gmail down")
        ), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
            code = cli.resummarize(self.config, since="2026-11-16", dry_run=False, assume_yes=True, legacy_only=False)
        # aaaa0002 is not stored and Gmail is down, so it fails too.
        self.assertEqual(code, 1)
        after = self.summaries()
        self.assertEqual(after["aaaa0001"], before["aaaa0001"])
        self.assertEqual(after["aaaa0002"], before["aaaa0002"])

    def test_legacy_only_selects_paragraph_rows(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            cli.resummarize(self.config, since="2026-11-15", dry_run=True, legacy_only=True)
        self.assertIn("0 email(s)", out.getvalue())

    def test_bad_date_is_rejected(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(cli.resummarize(self.config, since="Nov 15", dry_run=True), 1)


class HourlyRunTests(unittest.TestCase):
    """Hourly ingest: a day publishes only when the run that closes its window finishes."""

    def test_off_hour_runs_store_summaries_without_publishing_the_open_day(self) -> None:
        from newsletter_summary.digest import Publication

        eastern = _config(Path("/nonexistent")).gmail.tzinfo
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            with Store(config.storage.sqlite_path) as store:
                store.add_summary(_message("aaaa0001", received="2026-11-22T09:00:00-05:00"), _summary())
                # Summarized by the 10 AM run.
                store.connection.execute("update feed_items set published_at = '2026-11-22T15:00:03+00:00'")
                store.connection.commit()
                summaries = store.list_summaries()
                publication: Publication | None = None
                for hour in range(10, 19):
                    publication = publish(
                        summaries, timezone=eastern, hour=19, now=datetime(2026, 11, 22, hour, 0, 5, tzinfo=eastern),
                        publication=publication,
                    )
                    self.assertNotIn(datetime(2026, 11, 22).date(), publication.days, hour)
                closing = datetime(2026, 11, 22, 19, 0, 5, tzinfo=eastern)
                publication = publish(summaries, timezone=eastern, hour=19, now=closing, publication=publication)
                self.assertEqual(publication.days[datetime(2026, 11, 22).date()], closing)
                store.set_setting("digest_publication", publication.dumps())
                # Before the closing run finished, the day is not in the feed.
                self.assertEqual(load_digests(store, config, now=closing - timedelta(seconds=10)), [])
                self.assertEqual(
                    [digest.day.isoformat() for digest in load_digests(store, config, now=closing)], ["2026-11-22"]
                )


if __name__ == "__main__":
    unittest.main()
