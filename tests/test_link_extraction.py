from __future__ import annotations

import base64
import json
import unittest

from newsletter_summary.email_extract import (
    email_to_markdown,
    extract_links,
    extract_web_version_links,
    normalize_url,
)


def _substack_redirect(destination: str) -> str:
    """Substack's real click URL: /redirect/2/<base64url JSON with "e">.<signature>?j=..."""
    payload = {"e": destination, "p": 178123456, "s": 1234567, "f": True, "u": 98765432, "jwtPub": 2001}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"https://substack.com/redirect/2/{encoded}.dGhpcy1pcy1hLXNpZ25hdHVyZQ?j=eyJ1Ijo5ODc2NTQzMn0"


def _urls(html: str) -> list[str]:
    return [link.url for link in extract_links(html, "")]


class LinkFilterTests(unittest.TestCase):
    def test_finds_web_version_links_that_article_extraction_drops(self) -> None:
        html = """
        <a href="https://example.com/p/issue-12">View in browser</a>
        <a href="https://example.com/story">A story</a>
        <a href="https://example.com/unsubscribe">Unsubscribe</a>
        """
        self.assertEqual(
            [link.url for link in extract_web_version_links(html)], ["https://example.com/p/issue-12"]
        )
        self.assertEqual(_urls(html), ["https://example.com/story"])

    def test_drops_housekeeping_links(self) -> None:
        html = """
        <a href="https://example.com/unsubscribe">Unsubscribe</a>
        <a href="https://example.com/privacy-policy">Privacy Policy</a>
        <a href="https://example.com/terms">Terms of Service</a>
        <a href="https://example.com/manage-preferences">Manage preferences</a>
        <a href="https://ad.doubleclick.net/track">Sponsor</a>
        """
        self.assertEqual(_urls(html), [])

    def test_keeps_articles_whose_words_resemble_housekeeping_links(self) -> None:
        html = """
        <a href="https://example.com/blog/how-privacy-law-changed-in-2026">How privacy law changed in 2026</a>
        <a href="https://example.com/news/the-terms-of-the-ai-deal">The terms of the AI deal</a>
        <a href="https://example.com/2026/user-preferences-in-product-design">Designing for user preferences</a>
        """
        self.assertEqual(
            _urls(html),
            [
                "https://example.com/blog/how-privacy-law-changed-in-2026",
                "https://example.com/news/the-terms-of-the-ai-deal",
                "https://example.com/2026/user-preferences-in-product-design",
            ],
        )

    def test_drops_non_http_and_deduplicates(self) -> None:
        html = """
        <a href="mailto:editor@example.com">Email us</a>
        <a href="#top">Top</a>
        <a href="https://example.com/story">Story</a>
        <a href="https://example.com/story">Story again</a>
        """
        self.assertEqual(_urls(html), ["https://example.com/story"])

    def test_unwraps_tracking_redirect(self) -> None:
        html = '<a href="https://click.example.net/r?url=https%3A%2F%2Fexample.com%2Freal">Real story</a>'
        self.assertEqual(_urls(html), ["https://example.com/real"])

    def test_unwraps_substack_redirect_2_links(self) -> None:
        url = _substack_redirect("https://www.example.com/p/the-real-article?utm_source=substack&utm_medium=email")
        self.assertEqual(normalize_url(url), "https://www.example.com/p/the-real-article")
        html = f'<a href="{url}">The real article</a>'
        self.assertEqual(_urls(html), ["https://www.example.com/p/the-real-article"])

    def test_falls_back_to_plain_text_links(self) -> None:
        links = extract_links("", "Read https://example.com/story today.")
        self.assertEqual([link.url for link in links], ["https://example.com/story"])


class NormalizeUrlTests(unittest.TestCase):
    def test_encoded_characters_in_ordinary_urls_are_preserved(self) -> None:
        for url in (
            "https://example.com/search?q=a%26b%3Dc",
            "https://example.com/100%25-done",
            "https://cdn.example.com/file.pdf?sig=ab%2Bcd%3D%3D&expires=1700000000",
            "https://example.com/page?title=Hello%23World",
            "https://example.com/caf%C3%A9?q=a+b",
        ):
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url), url)

    def test_share_links_with_a_url_parameter_are_not_unwrapped(self) -> None:
        for url in (
            "https://twitter.com/intent/tweet?text=hi&url=https%3A%2F%2Fexample.com%2Fpost",
            "https://www.linkedin.com/shareArticle?mini=true&url=https%3A%2F%2Fexample.com%2Fpost",
        ):
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url), url)

    def test_known_redirectors_are_unwrapped_and_only_the_inner_url_is_decoded(self) -> None:
        cases = {
            # Destination encoded once as a query value; its own %26 stays encoded.
            "https://click.convertkit-mail.com/r?url=https%3A%2F%2Fexample.com%2Fsearch%3Fq%3Da%2526b":
                "https://example.com/search?q=a%26b",
            # Encoded twice.
            "https://links.example.net/redirect?to=https%253A%252F%252Fexample.com%252Fstory":
                "https://example.com/story",
            "https://www.google.com/url?q=https://example.com/a%3Fb%3Dc&sa=D&source=email":
                "https://example.com/a?b=c",
            "https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.com%2Fevent&h=AT0abc":
                "https://example.com/event",
            # A redirect to a Substack redirect, with credentials on the way.
            "https://click.example.net/r?url=" + _substack_redirect("https://example.com/p/x?token=secret").replace(
                "&", "%26"
            ).replace("?", "%3F"): "https://example.com/p/x",
        }  # fmt: skip
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url), expected)

    def test_opaque_trackers_are_left_alone(self) -> None:
        for url in (
            "https://link.theverge.com/click/36190123.4567/abc123def456",
            "https://ctrk.klclick.com/l/01HXYZABCDEF_0123456789",
            "https://example.us1.list-manage.com/track/click?u=abc123&id=def456",
        ):
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url), url)

    def test_base64_destination_in_a_tracker_path_is_unwrapped(self) -> None:
        url = "https://link.theverge.com/click/36190123.4567/aHR0cHM6Ly93d3cudGhldmVyZ2UuY29tL3Rlc3Q/abc123"
        self.assertEqual(normalize_url(url), "https://www.theverge.com/test")

    def test_credentials_are_dropped_without_reencoding_the_rest(self) -> None:
        self.assertEqual(
            normalize_url("https://example.com/p/post?q=a%26b&utm_source=x&token=abc&ref=a+b"),
            "https://example.com/p/post?q=a%26b&ref=a+b",
        )


class PromptBodyTests(unittest.TestCase):
    def test_markdown_body_leaves_out_link_urls(self) -> None:
        html = (
            '<p>Read <a href="https://click.example.net/r?url=https%3A%2F%2Fexample.com%2Fa-very-long-tracking-url">'
            "the story</a> today.</p>"
        )
        markdown = email_to_markdown(html, "")
        self.assertIn("the story", markdown)
        self.assertNotIn("https://", markdown)


if __name__ == "__main__":
    unittest.main()
