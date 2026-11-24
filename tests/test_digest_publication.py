from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from newsletter_summary.digest import MissedMessage, Publication, build_digests, missed_messages, publish
from newsletter_summary.rss import render_digest_feed
from newsletter_summary.store import NewsletterSummary

EASTERN = ZoneInfo("America/New_York")
DAY = date(2026, 11, 20)  # window: Nov 19 7 PM to Nov 20 7 PM
CLOSE = datetime(2026, 11, 20, 19, 0, tzinfo=EASTERN)


def _summary(gmail_id: str, received: datetime, created: datetime) -> NewsletterSummary:
    return NewsletterSummary(
        id=f"id-{gmail_id}", title=f"Title {gmail_id}", url="https://example.com/p", publication="Daily",
        authors=[], bullets=["A point."], summary="", content_access="full", source_gmail_id=gmail_id,
        source_subject="Subject", source_sender="Daily <d@example.com>",
        source_gmail_url=f"https://mail.google.com/mail/u/0/#all/{gmail_id}", email_received_at=received,
        created_at=created,
    )  # fmt: skip


def _missed(gmail_id: str, received: datetime, status: str = "summary_failed") -> MissedMessage:
    return MissedMessage(
        gmail_id=gmail_id, subject=f"Missed {gmail_id}", sender="Weekly Letter <w@example.com>", status=status,
        detail="RuntimeError: 429", received_at=received,
    )  # fmt: skip


def _build(summaries, publication, now, missed=None):
    return build_digests(summaries, timezone=EASTERN, hour=19, now=now, missed=missed, publication=publication)


def _publish(summaries, publication, now, missed=None):
    return publish(summaries, timezone=EASTERN, hour=19, now=now, missed=missed, publication=publication)


class LateSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        # The record started well before; DAY is tracked.
        self.record = Publication(since=CLOSE - timedelta(days=10))
        self.on_time = _summary("aaaa0001", CLOSE - timedelta(hours=10), CLOSE + timedelta(minutes=2))

    def test_summaries_from_the_closing_run_are_on_time(self) -> None:
        publication = _publish([self.on_time], self.record, CLOSE + timedelta(minutes=5))
        self.assertEqual(publication.days[DAY], CLOSE + timedelta(minutes=5))
        digests = _build([self.on_time], publication, CLOSE + timedelta(minutes=6))
        self.assertEqual([(digest.day, len(digest.newsletters), digest.late) for digest in digests], [(DAY, 1, [])])

    def test_a_closed_day_is_hidden_until_its_run_publishes_it(self) -> None:
        # 7:01 PM: the run is still summarizing; readers must not get a partial entry.
        self.assertEqual(_build([self.on_time], self.record, CLOSE + timedelta(minutes=1)), [])

    def test_retry_after_publication_goes_to_the_next_digest_under_late(self) -> None:
        published = _publish([self.on_time], self.record, CLOSE + timedelta(minutes=5))
        # The next day's 7 PM run retries a failure from DAY and succeeds.
        retried = _summary("aaaa0002", CLOSE - timedelta(hours=3), CLOSE + timedelta(days=1, minutes=1))
        fresh = _summary("aaaa0003", CLOSE + timedelta(hours=5), CLOSE + timedelta(days=1, minutes=2))
        summaries = [self.on_time, retried, fresh]

        # Before the next run publishes, the late summary is not slipped into DAY.
        before = _build(summaries, published, CLOSE + timedelta(days=1, minutes=3))
        self.assertEqual([item.source_gmail_id for item in before[0].newsletters], ["aaaa0001"])
        self.assertEqual(before[0].late, [])

        now = CLOSE + timedelta(days=1, minutes=5)
        digests = _build(summaries, _publish(summaries, published, now), now)
        newest, older = digests
        self.assertEqual(newest.day, DAY + timedelta(days=1))
        self.assertEqual([item.source_gmail_id for item in newest.newsletters], ["aaaa0003"])
        self.assertEqual([(day, [item.source_gmail_id for item in items]) for day, items in newest.late],
                         [(DAY, ["aaaa0002"])])
        self.assertEqual([item.source_gmail_id for item in older.newsletters], ["aaaa0001"])
        self.assertEqual(newest.summary_count, 2)

    def test_same_evening_rerun_waits_for_the_next_digest_even_if_it_is_otherwise_empty(self) -> None:
        published = _publish([self.on_time], self.record, CLOSE + timedelta(minutes=5))
        rerun = _summary("aaaa0002", CLOSE - timedelta(hours=3), CLOSE + timedelta(hours=2))
        summaries = [self.on_time, rerun]
        published = _publish(summaries, published, CLOSE + timedelta(hours=2, minutes=1))
        self.assertEqual(list(published.days), [DAY])
        self.assertEqual(len(_build(summaries, published, CLOSE + timedelta(hours=3))), 1)

        now = CLOSE + timedelta(days=1, minutes=5)
        digests = _build(summaries, _publish(summaries, published, now), now)
        self.assertEqual(digests[0].day, DAY + timedelta(days=1))
        self.assertEqual(digests[0].newsletters, [])
        self.assertEqual([day for day, _ in digests[0].late], [DAY])

    def test_days_from_before_the_record_are_shown_and_never_late(self) -> None:
        legacy = Publication(since=CLOSE + timedelta(days=2))
        late_created = _summary("aaaa0002", CLOSE - timedelta(hours=3), CLOSE + timedelta(days=1))
        digests = _build([self.on_time, late_created], legacy, CLOSE + timedelta(days=2, hours=1))
        self.assertEqual([len(digest.newsletters) for digest in digests], [2])
        # Without any record (before the first run of this version), closed days show as before.
        self.assertEqual(len(_build([self.on_time], None, CLOSE + timedelta(minutes=1))), 1)

    def test_record_round_trips(self) -> None:
        publication = _publish([self.on_time], self.record, CLOSE + timedelta(minutes=5))
        restored = Publication.loads(publication.dumps())
        assert restored is not None
        self.assertEqual(restored.days, publication.days)
        self.assertEqual(restored.since, publication.since)
        self.assertIsNone(Publication.loads(""))


class NotSummarizedTests(unittest.TestCase):
    def test_failed_emails_are_listed_in_their_day_and_make_an_entry(self) -> None:
        record = Publication(since=CLOSE - timedelta(days=10))
        missed = [_missed("bbbb0001", CLOSE - timedelta(hours=2))]
        now = CLOSE + timedelta(minutes=5)
        digests = _build([], _publish([], record, now, missed), now, missed)
        self.assertEqual(len(digests), 1)
        self.assertEqual([message.gmail_id for message in digests[0].not_summarized], ["bbbb0001"])

        feed = render_digest_feed(
            digests, title="Digest", description="d", web_public_url="https://newsletters.example.com"
        )
        item = ET.fromstring(feed).find("./channel/item")
        assert item is not None
        self.assertEqual(item.findtext("title"), "Newsletters for Friday, November 20, 2026 (0 newsletters, 1 not summarized)")
        description = item.findtext("description") or ""
        self.assertIn("<h2 class=\"section\">Not summarized</h2>", description)
        self.assertIn('<a href="https://newsletters.example.com/item/bbbb0001">Missed bbbb0001</a>', description)
        self.assertIn("Weekly Letter", description)
        self.assertNotIn("w@example.com", description)
        self.assertIn("<em>Summary failed</em>", description)

    def test_links_fall_back_to_gmail_and_rows_without_dates_use_first_seen(self) -> None:
        rows = [
            {"gmail_id": "cccc0001", "subject": "", "sender": "", "status": "fetch_failed", "detail": "",
             "received_at": "", "first_seen_at": "2026-11-20T15:00:00+00:00"},
            {"gmail_id": "cccc0002", "subject": "No body", "sender": "X <x@example.com>",
             "status": "skipped_no_body", "detail": "", "received_at": "2026-11-20T09:00:00-05:00",
             "first_seen_at": "2026-11-21T00:00:00+00:00"},
        ]  # fmt: skip
        missed = missed_messages(rows)
        self.assertEqual([message.received_at.isoformat() for message in missed],
                         ["2026-11-20T15:00:00+00:00", "2026-11-20T09:00:00-05:00"])
        digests = _build([], None, CLOSE + timedelta(minutes=1), missed)
        description = ET.fromstring(
            render_digest_feed(digests, title="Digest", description="d", web_public_url="https://n.example.com")
        ).findtext("./channel/item/description") or ""
        self.assertIn('<a href="https://n.example.com/item/cccc0001">(unknown subject)</a>', description)
        # The site cannot show an email with no body, so that one links to Gmail.
        self.assertIn('<a href="https://mail.google.com/mail/u/0/#all/cccc0002">No body</a>', description)
        without_site = render_digest_feed(digests, title="Digest", description="d").decode()
        self.assertIn("https://mail.google.com/mail/u/0/#all/cccc0001", without_site)


if __name__ == "__main__":
    unittest.main()
