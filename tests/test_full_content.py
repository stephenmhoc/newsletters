from __future__ import annotations

import base64
import unittest

from bs4 import BeautifulSoup

from newsletter_summary.email_extract import is_housekeeping_link
from newsletter_summary.full_content import clean_html, drop_leading_title, sanitize_email

_REDIRECT_TARGET = "https://example.substack.com/p/the-quiet-revolution"
_BASE64_TARGET = base64.urlsafe_b64encode(b"https://example.org/report.pdf").decode("ascii").rstrip("=")

# A hand-written imitation of a Substack issue: hidden preview text, a header
# image, tracking redirects, a linked video thumbnail, a lazy image, an image
# grid table, a tracking pixel, and a housekeeping footer.
SUBSTACK_HTML = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"><title>The Quiet Revolution</title>
  <style>.post {{ color: #333 }}</style>
  <script>window.track = true;</script>
</head>
<body>
  <div class="preview" style="display: none; max-height: 0px; overflow: hidden; mso-hide: all;">
    Why small tools win, and what it means for you&nbsp;&zwnj;
  </div>
  <table class="email-body" width="100%" role="presentation">
    <tr><td>
      <p class="view-online"><a href="https://example.substack.com/p/the-quiet-revolution?view=browser">View in browser</a></p>
      <img class="header" src="https://substackcdn.com/image/fetch/w_1100/header.png" width="550" height="220" alt="The Example Letter">
      <h1 class="post-title" id="title" onclick="steal()">The Quiet Revolution</h1>
      <h3>Why small tools win</h3>
      <p style="font-size:16px;color:#333">Last week I wrote about <strong>craft</strong> and <em>patience</em>.
        Read the <a href="https://email.mg1.substack.com/c/redirect?url={_REDIRECT_TARGET.replace(':', '%3A').replace('/', '%2F')}&amp;sig=abc">full essay</a>
        and the <a href="https://links.example.com/CL0/{_BASE64_TARGET}/1/abc">report</a>.</p>
      <p><a href="javascript:alert('xss')">Click me</a> <span onmouseover="steal()">hover</span></p>
      <blockquote><p>Tools shape the hands that use them.</p></blockquote>
      <ul><li>First idea</li><li>Second idea</li></ul>
      <ol><li>Step one</li></ol>
      <a href="https://www.youtube.com/watch?v=abc123"><img src="https://substackcdn.com/image/youtube/abc123.jpg" alt="Watch the talk" width="480" height="270"></a>
      <img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" data-src="https://substackcdn.com/image/lazy-chart.png" alt="Chart">
      <img srcset="https://substackcdn.com/image/w_424/photo.jpg 424w, https://substackcdn.com/image/w_1272/photo.jpg 1272w" alt="Photo">
      <picture><source srcset="https://substackcdn.com/image/w_800/diagram.webp" type="image/webp"><img alt="Diagram"></picture>
      <img src="cid:logo@example" alt="Example logo">
      <img src="cid:spacer@example">
      <table class="grid" width="600"><tr>
        <td width="50%" valign="top" style="padding:8px"><img src="https://substackcdn.com/image/left.jpg" alt="Left" width="280"></td>
        <td width="50%" valign="top" colspan="1"><p>Right column text</p></td>
      </tr></table>
      <iframe src="https://evil.example/frame"></iframe>
      <form action="https://evil.example/collect"><input name="email"><button>Subscribe</button></form>
      <img src="https://email.mg1.substack.com/o/open.gif" width="1" height="1" alt="">
      <img src="https://tracker.example/pixel.png" style="width:1px;height:1px;border:0">
      <!-- a comment with secrets -->
    </td></tr>
    <tr><td class="footer">
      <p>You're on the free list for Example Letter. <a href="https://example.substack.com/action/disable_email">Unsubscribe</a></p>
      <p><a href="https://example.substack.com/account/manage-preferences">Manage preferences</a> |
         <a href="https://example.substack.com/unsubscribe?token=x">Unsubscribe</a></p>
    </td></tr>
  </table>
</body>
</html>"""


class SanitizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = sanitize_email(SUBSTACK_HTML, "plain text fallback")
        self.html = self.result.html
        self.soup = BeautifulSoup(self.html, "html.parser")

    def image_sources(self) -> list[str]:
        return [str(image.get("src")) for image in self.soup.find_all("img")]

    def test_scripts_event_handlers_and_javascript_links_are_removed(self) -> None:
        lowered = self.html.lower()
        for fragment in ("<script", "window.track", "onclick", "onmouseover", "javascript:", "<style", "<iframe",
                         "<form", "<input", "<button", "secrets", "class=", "id="):
            self.assertNotIn(fragment, lowered)
        self.assertIn("Click me", self.html)

    def test_tracking_pixels_are_dropped(self) -> None:
        sources = self.image_sources()
        self.assertNotIn("https://email.mg1.substack.com/o/open.gif", sources)
        self.assertNotIn("https://tracker.example/pixel.png", sources)
        self.assertFalse(any(source.startswith(("data:", "cid:")) for source in sources))

    def test_normal_and_linked_images_keep_their_src(self) -> None:
        header = self.soup.find("img", src="https://substackcdn.com/image/fetch/w_1100/header.png")
        assert header is not None
        self.assertEqual(header.get("alt"), "The Example Letter")
        self.assertEqual(header.get("width"), "550")
        self.assertEqual(header.get("style"), "max-width:100%;height:auto")
        thumbnail = self.soup.find("img", src="https://substackcdn.com/image/youtube/abc123.jpg")
        assert thumbnail is not None
        assert thumbnail.parent is not None
        self.assertEqual(thumbnail.parent.name, "a")
        self.assertEqual(thumbnail.parent.get("href"), "https://www.youtube.com/watch?v=abc123")

    def test_lazy_and_srcset_images_are_promoted(self) -> None:
        sources = self.image_sources()
        self.assertIn("https://substackcdn.com/image/lazy-chart.png", sources)
        self.assertIn("https://substackcdn.com/image/w_1272/photo.jpg", sources)
        self.assertIn("https://substackcdn.com/image/w_800/diagram.webp", sources)
        self.assertNotIn("srcset", self.html)

    def test_inline_cid_images_become_alt_text(self) -> None:
        self.assertEqual(self.result.cid_images, 2)
        self.assertIn("Example logo", self.html)

    def test_unsubscribe_and_view_in_browser_links_are_removed(self) -> None:
        lowered = self.html.lower()
        self.assertNotIn("unsubscribe", lowered)
        self.assertNotIn("manage preferences", lowered)
        self.assertNotIn("view in browser", lowered)
        self.assertNotIn("disable_email", lowered)

    def test_redirect_links_are_unwrapped(self) -> None:
        hrefs = [anchor.get("href") for anchor in self.soup.find_all("a")]
        self.assertIn(_REDIRECT_TARGET, hrefs)
        self.assertIn("https://example.org/report.pdf", hrefs)
        self.assertFalse(any("mg1.substack.com" in str(href) for href in hrefs))
        self.assertIn('rel="noopener noreferrer"', self.html)

    def test_hidden_preheader_is_removed(self) -> None:
        self.assertNotIn("Why small tools win, and what it means", self.html)
        # Visible text that happens to repeat words is kept.
        self.assertIn("<h3>Why small tools win</h3>", self.html)

    def test_formatting_and_tables_are_kept(self) -> None:
        for fragment in ("<h1>The Quiet Revolution</h1>", "<strong>craft</strong>", "<em>patience</em>",
                         "<blockquote>", "<ul><li>First idea</li>", "<ol><li>Step one</li></ol>"):
            self.assertIn(fragment, self.html)
        # RSS readers ignore our CSS, so layout tables become plain blocks.
        self.assertIsNone(self.soup.find("table"))
        self.assertIsNone(self.soup.find("td"))
        left = self.soup.find("img", alt="Left")
        assert left is not None
        self.assertIn("Right column text", self.html)
        self.assertLess(self.html.index('alt="Left"'), self.html.index("Right column text"))

    def test_data_tables_are_kept(self) -> None:
        html = sanitize_email(
            "<table><tr><th>Team</th><th>Wins</th></tr><tr><td>Pistons</td><td>12</td></tr></table>", ""
        ).html
        self.assertIn("<table>", html)
        self.assertIn("<th>Team</th>", html)
        self.assertIn("<td>12</td>", html)

    def test_plain_text_email_is_escaped_and_linkified(self) -> None:
        html = sanitize_email("", "Hello <friend> & co.\nSecond line\n\nRead https://example.com/post?a=1&b=2.").html
        self.assertIn("<p>Hello &lt;friend&gt; &amp; co.<br>Second line</p>", html)
        self.assertIn(
            '<a href="https://example.com/post?a=1&amp;b=2" rel="noopener noreferrer">'
            "https://example.com/post?a=1&amp;b=2</a>.",
            html,
        )

    def test_render_pass_is_idempotent_and_still_strips_unsafe_html(self) -> None:
        self.assertEqual(clean_html(self.html), self.html)
        dirty = '<img src="https://a.example/x.png" onerror="x()" style="position:fixed;max-width:100%"><script>x()</script>'
        self.assertEqual(clean_html(dirty), '<img src="https://a.example/x.png" style="max-width:100%">')


class HousekeepingLinkTests(unittest.TestCase):
    def test_classifies_housekeeping_links(self) -> None:
        self.assertTrue(is_housekeeping_link("Unsubscribe", "https://example.com/u/123"))
        self.assertTrue(is_housekeeping_link("", "https://example.com/manage-preferences"))
        self.assertTrue(is_housekeeping_link("View this email in your browser", "https://example.com/x"))
        self.assertTrue(is_housekeeping_link("", "mailto:unsubscribe@example.com"))
        self.assertFalse(is_housekeeping_link("Write back", "mailto:editor@example.com"))
        self.assertFalse(is_housekeeping_link("The terms of the AI deal", "https://example.com/the-terms-of-the-ai-deal"))



class EmailChromeTests(unittest.TestCase):
    def test_substack_chrome_and_icons_are_removed_but_content_images_stay(self) -> None:
        html = """<html><body>
<div><span>Forwarded this email? <a href="https://example.substack.com/subscribe">Subscribe here</a> for more</span></div>
<h1><a href="https://example.substack.com/p/post">The Post</a></h1>
<p><a href="https://substack.com/app-link/post?like=1"><img src="https://substackcdn.com/icon/heart.png" width="18" height="18"></a>
   <a href="https://substack.com/app-link/post?share=1"><span>Share</span></a>
   <a href="https://open.substack.com/pub/x/p/post"><span>READ IN APP</span></a></p>
<p>The actual argument of the post.</p>
<img src="https://substackcdn.com/image/chart.png" width="550" height="345" alt="A chart">
<a href="https://substack.com/signup"><img src="https://substack.com/img/email/start-your-substack-button@2x.png" width="183" height="40" alt="Start your Substack"></a>
</body></html>"""
        out = sanitize_email(html, "").html
        self.assertNotIn("Forwarded this email", out)
        self.assertNotIn("READ IN APP", out)
        self.assertNotIn(">Share<", out)
        self.assertNotIn("heart.png", out)
        self.assertNotIn("start-your-substack", out)
        self.assertIn("chart.png", out)
        self.assertIn("The actual argument of the post.", out)

    def test_leading_title_is_dropped_only_when_it_repeats_the_title(self) -> None:
        html = '<h1><a href="https://example.com">Why Everything Is Breaking</a></h1><p>Body.</p>'
        self.assertNotIn("<h1>", drop_leading_title(html, "Why everything is breaking!"))
        self.assertIn("Body.", drop_leading_title(html, "Why everything is breaking!"))
        self.assertEqual(drop_leading_title(html, "Another title"), html)


class CredentialStrippingTests(unittest.TestCase):
    def test_subscriber_tokens_and_tracking_are_dropped(self) -> None:
        from newsletter_summary.email_extract import strip_credentials

        url = (
            "https://substack.com/app-link/post?publication_id=2880588&post_id=225684445"
            "&utm_source=post-email-title&isFreemail=true&r=2df0mh&token=eyJhbGciOi.x.y"
        )
        self.assertEqual(
            strip_credentials(url), "https://substack.com/app-link/post?publication_id=2880588&post_id=225684445"
        )
        self.assertEqual(
            strip_credentials("https://stratechery.com/2026/x/?access_token=abc123"), "https://stratechery.com/2026/x/"
        )
        self.assertEqual(strip_credentials("https://example.com/p/a-post"), "https://example.com/p/a-post")


if __name__ == "__main__":
    unittest.main()
