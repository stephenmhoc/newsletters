from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from newsletter_summary.digest import DailyDigest, canonical_publications
from newsletter_summary.http_util import etag_for, is_not_modified
from newsletter_summary.render import newsletter_entry, reading_time
from newsletter_summary.rss import render_all_feed, render_digest_feed
from newsletter_summary.store import NewsletterSummary

EASTERN = ZoneInfo("America/New_York")
SITE = "https://newsletters.example.com"
GMAIL_ID = "18c0ffee00000001"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"


def _summary(gmail_id: str = GMAIL_ID, **overrides) -> NewsletterSummary:
    values: dict[str, Any] = {
        "id": f"id-{gmail_id}",
        "title": "Big <news>",
        "url": "https://example.com/p/big-news",
        "publication": "Daily Dispatch",
        "authors": ["Ada Writer"],
        "bullets": ["First point.", "Second point."],
        "summary": "",
        "content_access": "full",
        "source_gmail_id": gmail_id,
        "source_subject": "Issue",
        "source_sender": "Daily Dispatch <editor@example.com>",
        "source_gmail_url": f"https://mail.google.com/mail/u/0/#all/{gmail_id}",
        "email_received_at": datetime(2026, 10, 14, 8, 0, tzinfo=EASTERN),
        "word_count": 1200,
    }
    values.update(overrides)
    return NewsletterSummary(**values)


class EntryTests(unittest.TestCase):
    def test_title_is_an_h3_linked_to_the_reading_view_with_original_secondary(self) -> None:
        html = newsletter_entry(_summary(), site=SITE)
        self.assertTrue(html.startswith(f'<h3><a href="{SITE}/item/{GMAIL_ID}">Big &lt;news&gt;</a></h3>'))
        self.assertIn(
            '<p class="meta"><strong>Daily Dispatch</strong> · by Ada Writer · 6 min read</p>', html
        )
        self.assertIn("<ul><li>First point.</li><li>Second point.</li></ul>", html)
        self.assertIn(
            f'<a href="https://example.com/p/big-news">Original</a> · <a href="{SITE}/read/{GMAIL_ID}">Read in full</a>',
            html,
        )

    def test_without_the_site_titles_link_to_the_article_and_there_is_no_reading_list(self) -> None:
        html = newsletter_entry(_summary(), site=None)
        self.assertIn('<h3><a href="https://example.com/p/big-news">Big &lt;news&gt;</a></h3>', html)
        self.assertNotIn("/read/", html)
        self.assertNotIn("Original", html)

    def test_fallback_urls_are_not_shown_as_the_original(self) -> None:
        for url in (f"{SITE}/item/{GMAIL_ID}", f"https://mail.google.com/mail/u/0/#all/{GMAIL_ID}"):
            with self.subTest(url=url):
                self.assertNotIn("Original", newsletter_entry(_summary(url=url), site=SITE))

    def test_legacy_ids_and_paragraph_rows(self) -> None:
        html = newsletter_entry(_summary("legacy-id", bullets=[], summary="A paragraph."), site=SITE)
        self.assertIn('<h3><a href="https://example.com/p/big-news">', html)
        self.assertIn("<p>A paragraph.</p>", html)
        self.assertNotIn("/read/", html)

    def test_reading_time(self) -> None:
        self.assertEqual(reading_time(0), "")
        self.assertEqual(reading_time(10), "1 min read")
        self.assertEqual(reading_time(238 * 3 + 1), "4 min read")


class DigestFeedFormatTests(unittest.TestCase):
    def test_each_item_links_to_its_day_page_and_late_and_missed_link_to_the_reading_view(self) -> None:
        digest = DailyDigest(
            day=date(2026, 10, 14),
            start=datetime(2026, 10, 13, 19, tzinfo=EASTERN),
            end=datetime(2026, 10, 14, 19, tzinfo=EASTERN),
            newsletters=[_summary()],
            late=[(date(2026, 10, 13), [_summary("18c0ffee00000002", title="Late one")])],
        )
        feed = render_digest_feed([digest], title="Digest", description="d", web_public_url=SITE)
        item = ET.fromstring(feed).find("./channel/item")
        assert item is not None
        self.assertEqual(item.findtext("link"), f"{SITE}/digest/2026-10-14")
        description = item.findtext("description") or ""
        self.assertIn(f'<h3><a href="{SITE}/item/18c0ffee00000002">Late one</a></h3>', description)
        self.assertIn('<h2 class="section">Late from Tuesday, October 13</h2>', description)

    def test_last_build_date_comes_from_items_so_the_feed_is_stable(self) -> None:
        digest = DailyDigest(
            day=date(2026, 10, 14),
            start=datetime(2026, 10, 13, 19, tzinfo=EASTERN),
            end=datetime(2026, 10, 14, 19, tzinfo=EASTERN),
            newsletters=[_summary()],
        )
        first = render_digest_feed([digest], title="Digest", description="d")
        second = render_digest_feed([digest], title="Digest", description="d")
        self.assertEqual(first, second)
        self.assertEqual(ET.fromstring(first).findtext("./channel/lastBuildDate"), "Wed, 14 Oct 2026 19:00:00 -0400")


class AllFeedTests(unittest.TestCase):
    def test_items_have_the_publication_as_author_bullets_first_then_the_full_text(self) -> None:
        rewritten: list[str] = []

        def rewrite(url: str) -> str:
            rewritten.append(url)
            return "https://rss.example.com/img/sig/abc"

        content = '<h1>Big &lt;news&gt;</h1><p>Body text.</p><img src="https://cdn.example.com/a.png"><script>x</script>'
        feed = render_all_feed(
            [(_summary(content_access="partial"), content), (_summary("18c0ffee00000002", title="No body"), None)],
            self_url="https://rss.example.com/all/token/feed.xml",
            site=SITE,
            rewrite_image=rewrite,
        )
        channel = ET.fromstring(feed).find("channel")
        assert channel is not None
        self.assertEqual(channel.findtext("title"), "Newsletters: Everything")
        self.assertEqual(channel.findtext("image/url"), "https://rss.example.com/newsletters/icons/all.png")
        self.assertEqual(channel.findtext("link"), "https://rss.example.com/newsletters/all/")
        first, second = channel.findall("item")
        self.assertEqual(first.findtext("guid"), f"newsletter-all:{GMAIL_ID}")
        self.assertEqual(first.findtext("link"), f"{SITE}/item/{GMAIL_ID}")
        self.assertEqual(first.findtext(f"{DC_NS}creator"), "Daily Dispatch")
        self.assertEqual(first.findtext("pubDate"), "Wed, 14 Oct 2026 08:00:00 -0400")
        self.assertIn("<li>First point.</li>", first.findtext("description") or "")
        body = first.findtext(f"{CONTENT_NS}encoded") or ""
        self.assertLess(body.index("First point."), body.index("Body text."))
        self.assertIn(f'<a href="{SITE}/read/{GMAIL_ID}">Add to reading list</a>', body)
        self.assertIn("Excerpt only", body)
        self.assertIn('src="https://rss.example.com/img/sig/abc"', body)
        self.assertEqual(rewritten, ["https://cdn.example.com/a.png"])
        self.assertNotIn("<script", body)
        # The headline repeats the title, so it is dropped from the body.
        self.assertNotIn("<h1>", body)
        self.assertIn("full text of this email is not stored", second.findtext(f"{CONTENT_NS}encoded") or "")


class PublicationNameTests(unittest.TestCase):
    def _at(self, days: int, publication: str, sender: str = "The Backstory <hi@puck.example>") -> NewsletterSummary:
        return _summary(
            f"18c0ffee{days:08x}",
            publication=publication,
            source_sender=sender,
            email_received_at=datetime(2026, 11, 1, tzinfo=UTC) + timedelta(days=days),
        )

    def test_variants_from_one_sender_take_the_most_common_name(self) -> None:
        summaries = [
            self._at(0, "The Backstory"),
            self._at(1, "The Backstory (Puck and Air Mail)"),
            self._at(2, "The Backstory"),
            self._at(3, "the backstory"),
        ]
        names = [summary.publication for summary in canonical_publications(summaries)]
        self.assertEqual(names, ["The Backstory"] * 4)

    def test_distinct_newsletters_from_one_address_stay_distinct(self) -> None:
        sender = "The Times <newsletters@times.example>"
        summaries = [self._at(0, "DealBook", sender), self._at(1, "The Morning", sender), self._at(2, "The Morning", sender)]
        names = [summary.publication for summary in canonical_publications(summaries)]
        self.assertEqual(names, ["DealBook", "The Morning", "The Morning"])

    def test_ties_go_to_the_most_recent_and_overrides_win(self) -> None:
        summaries = [self._at(0, "Installer"), self._at(1, "Installer by The Verge")]
        self.assertEqual({s.publication for s in canonical_publications(summaries)}, {"Installer by The Verge"})
        overridden = canonical_publications(summaries, {"HI@PUCK.EXAMPLE": "Installer"})
        self.assertEqual({s.publication for s in overridden}, {"Installer"})

    def test_other_senders_and_empty_names_are_untouched(self) -> None:
        summaries = [self._at(0, "The Backstory"), self._at(1, "The Backstory Weekly", "Other <x@other.example>"),
                     self._at(2, "", "")]
        names = [summary.publication for summary in canonical_publications(summaries)]
        self.assertEqual(names, ["The Backstory", "The Backstory Weekly", ""])
        # Unchanged summaries are the same objects.
        self.assertIs(canonical_publications(summaries)[0], summaries[0])
        self.assertEqual(dataclasses.replace(summaries[0]), summaries[0])


class ConditionalGetTests(unittest.TestCase):
    def test_etag_wins_and_weak_tags_match(self) -> None:
        etag = etag_for(b"feed")
        modified = datetime(2026, 10, 14, 23, 0, tzinfo=UTC)
        self.assertTrue(is_not_modified({"If-None-Match": etag}, etag, modified))
        self.assertTrue(is_not_modified({"If-None-Match": f'"other", W/{etag}'}, etag, modified))
        self.assertTrue(is_not_modified({"If-None-Match": "*"}, etag, modified))
        # A non-matching ETag is modified even when the date matches.
        self.assertFalse(
            is_not_modified({"If-None-Match": '"other"', "If-Modified-Since": format_datetime(modified, usegmt=True)},
                            etag, modified)
        )

    def test_if_modified_since_without_an_etag(self) -> None:
        etag = etag_for(b"feed")
        modified = datetime(2026, 10, 14, 23, 0, 30, 500, tzinfo=UTC)
        self.assertTrue(is_not_modified({"If-Modified-Since": "Wed, 14 Oct 2026 23:00:30 GMT"}, etag, modified))
        self.assertFalse(is_not_modified({"If-Modified-Since": "Wed, 14 Oct 2026 22:00:00 GMT"}, etag, modified))
        self.assertFalse(is_not_modified({"If-Modified-Since": "garbage"}, etag, modified))
        self.assertFalse(is_not_modified({}, etag, modified))
        self.assertFalse(is_not_modified({"If-Modified-Since": "Wed, 14 Oct 2026 23:00:30 GMT"}, etag, None))


if __name__ == "__main__":
    unittest.main()
