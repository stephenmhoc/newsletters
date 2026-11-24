"""HTML fragments shared by the feeds and the private web UI.

One renderer for a digest's newsletter entries, so the feed and the site never
drift apart. Links depend on where the HTML is shown:

- site=None: no reading view (the web UI is off); titles link to the article.
- site="": the private site itself; links are relative.
- site="https://newsletters.example.com": absolute links for feed readers.

Every value is escaped here; stored email HTML is never passed through.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from html import escape

from .full_content import reading_minutes
from .gmail_client import GMAIL_ID_PATTERN, gmail_link, sender_name
from .store import FullItem, NewsletterSummary

STATUS_LABELS = {
    "summarized": "Summarized",
    "skipped_no_links": "Skipped: no links",
    "skipped_no_body": "Skipped: no body",
    "summary_failed": "Summary failed",
    "fetch_failed": "Fetch failed",
}
ADD_LABEL = "Add to reading list"
# Shown for a web page whose article could not be fully extracted (content_access partial).
WEB_THIN_NOTE = (
    "Couldn't extract the full article, perhaps because of a paywall or a JavaScript-only page; open the original."
)


def item_url(site: str, gmail_id: str) -> str:
    """The reading view for any processed email."""
    return f"{site.rstrip('/')}/item/{gmail_id}"


def read_url(site: str, gmail_id: str) -> str:
    """One click: add to the reading list, then open the reading view."""
    return f"{site.rstrip('/')}/read/{gmail_id}"


def digest_page_url(site: str, day: date) -> str:
    return f"{site.rstrip('/')}/digest/{day.isoformat()}"


def original_url(url: str, site: str | None) -> str:
    """The article's own URL, not a Gmail or reading-view fallback."""
    if not url.startswith(("http://", "https://")) or url.startswith("https://mail.google.com/"):
        return ""
    return "" if site and url.startswith(f"{site.rstrip('/')}/") else url


def reading_time(words: int) -> str:
    minutes = reading_minutes(words)
    return f"{minutes} min read" if minutes else ""


def long_date(day: date) -> str:
    return f"{day:%A, %B} {day.day}, {day.year}"


def late_heading(day: date) -> str:
    return f"Late from {day:%A, %B} {day.day}"


def clock(moment: datetime) -> str:
    return f"{moment:%a} {moment.hour % 12 or 12}:{moment:%M %p}"


def link(url: str, label: str, *, new_tab: bool = False, css: str = "") -> str:
    """An escaped link; only http(s) and site-relative URLs become links."""
    if not url.startswith(("http://", "https://", "/")):
        return escape(label)
    attributes = f' class="{css}"' if css else ""
    if new_tab:
        attributes += ' rel="noopener noreferrer" target="_blank"'
    return f'<a href="{escape(url)}"{attributes}>{escape(label)}</a>'


def publication_name(newsletter: NewsletterSummary) -> str:
    return newsletter.publication or sender_name(newsletter.source_sender)


def newsletter_meta(newsletter: NewsletterSummary) -> str:
    """Publication · by authors · N min read · partial preview."""
    details = [f"<strong>{escape(publication_name(newsletter))}</strong>"]
    authors = [author for author in newsletter.authors if author.casefold() != newsletter.publication.casefold()]
    if authors:
        details.append(f"by {escape(', '.join(authors))}")
    minutes = reading_time(newsletter.word_count)
    if minutes:
        details.append(escape(minutes))
    if newsletter.content_access == "partial":
        details.append("<em>partial preview</em>")
    return " · ".join(details)


def key_points(newsletter: NewsletterSummary) -> str:
    if newsletter.bullets:
        return "<ul>" + "".join(f"<li>{escape(bullet)}</li>" for bullet in newsletter.bullets) + "</ul>"
    if newsletter.summary:
        return f"<p>{escape(newsletter.summary)}</p>"
    return ""


def newsletter_entry(
    newsletter: NewsletterSummary,
    *,
    site: str | None,
    actions: list[str] | None = None,
    new_tab: bool = False,
) -> str:
    """One newsletter: an <h3> title linked to the reading view, its meta line, bullets, and links.

    actions replaces the feed's default "Read in full" link (the web UI
    passes its own buttons).
    """
    gmail_id = newsletter.source_gmail_id
    readable = site is not None and bool(GMAIL_ID_PATTERN.match(gmail_id))
    title_href = item_url(site or "", gmail_id) if readable else newsletter.url
    original = original_url(newsletter.url, site)
    links = []
    if readable and original:
        links.append(link(original, "Original", new_tab=new_tab))
    if actions is not None:
        links.extend(actions)
    elif readable:
        links.append(link(read_url(site or "", gmail_id), "Read in full"))
    parts = [
        f"<h3>{link(title_href, newsletter.title, new_tab=new_tab and not readable)}</h3>",
        f'<p class="meta">{newsletter_meta(newsletter)}</p>',
        key_points(newsletter),
    ]
    if links:
        # The web UI's actions include forms, which may not sit inside a <p>.
        tag = "div" if actions is not None else "p"
        parts.append(f'<{tag} class="actions">{" · ".join(links)}</{tag}>')
    return "\n".join(part for part in parts if part)


def missed_message_html(message, site: str | None) -> str:
    """Subject (linked), sender name, and why the email has no summary."""
    gmail_id = message.gmail_id
    valid = bool(GMAIL_ID_PATTERN.match(gmail_id))
    # An email with no body cannot be shown by the reading view; Gmail can.
    if site is None or message.status == "skipped_no_body":
        href = gmail_link(gmail_id)
    else:
        href = item_url(site, gmail_id)
    subject = message.subject or "(unknown subject)"
    parts = [link(href, subject) if valid else escape(subject)]
    name = sender_name(message.sender)
    if name:
        parts.append(escape(name))
    parts.append(f"<em>{escape(STATUS_LABELS.get(message.status, message.status))}</em>")
    return " · ".join(parts)


def digest_body(
    digest,
    *,
    site: str | None,
    entry: Callable[[NewsletterSummary], str] | None = None,
    section_tag: str = "h2",
) -> str:
    """A digest's newsletters, its "Late from" sections, and "Not summarized"."""
    render = entry or (lambda newsletter: newsletter_entry(newsletter, site=site))
    sections = []
    if digest.newsletters:
        noun = "newsletter" if len(digest.newsletters) == 1 else "newsletters"
        sections.append(
            f'<p class="meta">{len(digest.newsletters)} {noun} received from {escape(clock(digest.start))} '
            f"to {escape(clock(digest.end))}.</p>"
        )
        sections.extend(_entry_block(render(newsletter)) for newsletter in digest.newsletters)
    for day, newsletters in digest.late:
        sections.append(f'<{section_tag} class="section">{escape(late_heading(day))}</{section_tag}>')
        sections.append('<p class="meta">Summarized after that day\'s digest was published.</p>')
        sections.extend(_entry_block(render(newsletter)) for newsletter in newsletters)
    if digest.not_summarized:
        sections.append(f'<{section_tag} class="section">Not summarized</{section_tag}>')
        sections.append(
            '<ul class="missed">'
            + "".join(f"<li>{missed_message_html(message, site)}</li>" for message in digest.not_summarized)
            + "</ul>"
        )
    return "\n".join(sections)


def _entry_block(html: str) -> str:
    return f'<div class="entry">\n{html}\n</div>'


def digest_title(digest) -> str:
    count = digest.summary_count
    noun = "newsletter" if count == 1 else "newsletters"
    counts = f"{count} {noun}"
    if digest.not_summarized:
        counts += f", {len(digest.not_summarized)} not summarized"
    return f"Newsletters for {long_date(digest.day)} ({counts})"


def full_item_byline(item: FullItem) -> str:
    details = []
    if item.publication:
        details.append(f"<strong>{escape(item.publication)}</strong>")
    # Personal newsletters often have the author as the publication.
    authors = [author for author in item.authors if author.casefold() != item.publication.casefold()]
    if authors:
        details.append(f"by {escape(', '.join(authors))}")
    return " · ".join(details)


def excerpt_note(content_access: str, *, web: bool = False) -> str:
    if content_access != "partial":
        return ""
    if web:
        return f"<p><em>{escape(WEB_THIN_NOTE)}</em></p>"
    return "<p><em>Excerpt only: this issue is a paid preview, so the email contains only part of the article.</em></p>"
