"""Every processed email's content, stored at ingest in email_content.

`run` saves each email's raw HTML/text with its sanitized rendering, so the
reading view, the reading list feed, and the everything feed never wait on
Gmail. When SANITIZER_VERSION is bumped, the sanitized HTML is re-derived from
the stored raw email: lazily on read, or for every row with
`newsletter-summary resanitize`.

On PostgreSQL the table needs deploy/sql/2026-11-24-email-content.sql. Until
then the store raises StoreFeatureUnavailable and callers fall back to Gmail
(the reading view) or to full_items.content_html (the reading list).
"""
from __future__ import annotations

import logging
from dataclasses import replace

from .full_content import SANITIZER_VERSION, sanitize_email, word_count
from .gmail_client import NewsletterMessage
from .store import CONTENT_MIGRATION, FullItem, Store, StoredContent, StoreFeatureUnavailable


def content_from_message(message: NewsletterMessage) -> StoredContent:
    sanitized = sanitize_email(message.html, message.text)
    return StoredContent(
        gmail_id=message.gmail_id,
        subject=message.subject,
        sender=message.sender,
        received_at=message.received_at.isoformat(),
        raw_html=message.html,
        raw_text=message.text,
        content_html=sanitized.html,
        sanitizer_version=SANITIZER_VERSION,
        word_count=word_count(sanitized.html),
    )


def resanitized(content: StoredContent) -> StoredContent:
    """The content re-derived from its stored raw email with the current sanitizer."""
    html = sanitize_email(content.raw_html, content.raw_text).html
    return replace(content, content_html=html, sanitizer_version=SANITIZER_VERSION, word_count=word_count(html))


def is_stale(content: StoredContent) -> bool:
    return content.sanitizer_version < SANITIZER_VERSION and bool(content.raw_html or content.raw_text)


def current_content(store: Store, gmail_id: str) -> StoredContent | None:
    """Stored content, re-sanitized (and saved back) when an older sanitizer produced it.

    None when nothing is stored, including before the migration.
    """
    try:
        content = store.get_content(gmail_id)
    except StoreFeatureUnavailable:
        return None
    if content is None or not is_stale(content):
        return content
    fresh = resanitized(content)
    try:
        store.update_sanitized(
            gmail_id,
            content_html=fresh.content_html,
            sanitizer_version=fresh.sanitizer_version,
            word_count=fresh.word_count,
        )
    except Exception:
        # Serving the fresh HTML matters more than saving it.
        logging.exception("Could not save re-sanitized content for %s", gmail_id)
    return fresh


def save_message(store: Store, message: NewsletterMessage) -> StoredContent:
    """Sanitize and store a fetched email; raises StoreFeatureUnavailable before the migration."""
    content = content_from_message(message)
    store.save_content(content)
    return content


def resanitize_all(store: Store, *, force: bool = False) -> tuple[int, int]:
    """Re-derive stored content; returns (re-sanitized, skipped without a raw body)."""
    ids = store.content_ids() if force else store.stale_content_ids(SANITIZER_VERSION)
    done = skipped = 0
    for gmail_id in ids:
        content = store.get_content(gmail_id)
        if content is None or not (content.raw_html or content.raw_text):
            skipped += 1
            continue
        fresh = resanitized(content)
        store.update_sanitized(
            gmail_id,
            content_html=fresh.content_html,
            sanitizer_version=fresh.sanitizer_version,
            word_count=fresh.word_count,
        )
        done += 1
    return done, skipped


def with_stored_content(store: Store, items: list[FullItem]) -> list[FullItem]:
    """Reading list items with their content from email_content.

    full_items keeps membership and metadata. Items added before content was
    stored keep their own content_html, which stays the fallback.
    """
    resolved = []
    for item in items:
        content = current_content(store, item.gmail_id)
        resolved.append(replace(item, content_html=content.content_html) if content else item)
    return resolved


class ContentWriter:
    """Best-effort content writes during `run`: a failure never fails the run."""

    def __init__(self, store: Store):
        self.store = store
        self.available = True
        self.saved = 0

    def save(self, message: NewsletterMessage) -> None:
        if not self.available or not message.has_body:
            return
        try:
            save_message(self.store, message)
            self.saved += 1
        except StoreFeatureUnavailable:
            logging.warning("email_content is missing; apply %s to store email content", CONTENT_MIGRATION)
            self.available = False
        except Exception:
            logging.exception("Could not store the content of %s; continuing", message.gmail_id)
