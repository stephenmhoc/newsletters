from __future__ import annotations

import fcntl
import hashlib
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import _postgres
from .email_extract import strip_credentials
from .gmail_client import NewsletterMessage, unify_gmail_link
from .openrouter import SummaryItem

# Bumped whenever _migrate needs to run again on an already-initialized database.
SCHEMA_VERSION = 6

# A summary that fails this many times (message_log.attempts) is given up on,
# so one bad email cannot pin the retry window (and spend a model call) forever.
MAX_SUMMARY_ATTEMPTS = 3

# Outcomes recorded in message_log by `run`.
MESSAGE_STATUSES = frozenset(
    {"summarized", "skipped_no_links", "skipped_no_body", "summary_failed", "fetch_failed"}
)
# Outcomes that leave an email out of the digest; listed under "Not summarized".
UNSUMMARIZED_STATUSES = ("skipped_no_links", "skipped_no_body", "summary_failed", "fetch_failed")

# PostgreSQL needs these applied by the database owner; until then the
# features that use them are off and everything else keeps working.
FULL_TEXT_MIGRATION = "deploy/sql/2026-11-full-text.sql"
ATTEMPTS_MIGRATION = "deploy/sql/2026-11-23-message-attempts.sql"
CONTENT_MIGRATION = "deploy/sql/2026-11-24-email-content.sql"
WEB_MIGRATION = "deploy/sql/2026-11-25-web-links.sql"
# (feature, table, column probed, migration) for the /status page.
MIGRATIONS = (
    ("Reading list", "full_items", "gmail_id", FULL_TEXT_MIGRATION),
    ("Email log", "message_log", "gmail_id", FULL_TEXT_MIGRATION),
    ("Summary attempts", "message_log", "attempts", ATTEMPTS_MIGRATION),
    ("Stored email content", "email_content", "sanitizer_version", CONTENT_MIGRATION),
    ("Web links on the reading list", "full_items", "source_url", WEB_MIGRATION),
)

# Session-level PostgreSQL advisory lock that serializes `run`.
RUN_LOCK_KEY = 0x6E6577736C657474  # "newslett"

# The hourly `run` writes while the long-lived `serve` process reads, so wait for
# the other side to finish instead of failing with "database is locked".
BUSY_TIMEOUT_MS = 5_000


@dataclass(frozen=True)
class NewsletterSummary:
    id: str
    title: str
    url: str
    publication: str
    authors: list[str]
    bullets: list[str]
    # Paragraph summary from before bullets; shown only for older rows.
    summary: str
    content_access: str
    source_gmail_id: str
    source_subject: str
    source_sender: str
    source_gmail_url: str
    email_received_at: datetime
    # When this service stored the summary (feed_items.published_at).
    created_at: datetime | None = None
    # Words in the stored email content; 0 when unknown.
    word_count: int = 0


@dataclass(frozen=True)
class FullItem:
    """A reading list item: a newsletter email, or a web page added with "Add a link".

    gmail_id is the item's ID: a Gmail message ID, or "web-<hash>" for a web
    page (source_kind "web"), which never reaches Gmail.
    """

    gmail_id: str
    title: str
    url: str
    publication: str
    authors: list[str]
    content_access: str
    source_gmail_url: str
    bullets: list[str]
    content_html: str
    email_received_at: str
    added_at: str
    # "gmail" or "web". Web items need deploy/sql/2026-11-25-web-links.sql.
    source_kind: str = "gmail"
    # The URL as submitted (normalized), for finding a saved page before fetching it.
    source_url: str = ""

    @property
    def is_web(self) -> bool:
        return self.source_kind == "web"


@dataclass(frozen=True)
class StoredContent:
    """One email's body as fetched at ingest, plus its sanitized rendering.

    raw_html/raw_text are kept so a newer sanitizer can re-derive
    content_html; sanitizer_version records which one produced it.
    """

    gmail_id: str
    subject: str
    sender: str
    received_at: str
    raw_html: str
    raw_text: str
    content_html: str
    sanitizer_version: int
    word_count: int
    stored_at: str = ""


class StoreFeatureUnavailable(RuntimeError):
    """A table or column is missing, e.g. before a PostgreSQL migration is applied."""


class RunLockBusy(RuntimeError):
    """Another `run` holds the run lock."""


def is_missing_schema_error(exc: BaseException) -> bool:
    """True only when SQL failed because a table or column does not exist.

    Timeouts, dropped connections, and other errors are not "not migrated".
    """
    # PostgreSQL undefined_table / undefined_column.
    if getattr(exc, "sqlstate", None) in {"42P01", "42703"}:
        return True
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).lower()
        return message.startswith(("no such table", "no such column")) or "has no column named" in message
    return False


@contextmanager
def run_lock(sqlite_path: Path) -> Iterator[None]:
    """Hold an exclusive lock for one `run`, or raise RunLockBusy at once.

    PostgreSQL uses a session-level advisory lock, released when its
    connection closes; SQLite uses a file lock beside the database. Either way
    a crashed run releases the lock automatically.
    """
    if _postgres.configured():
        connection = _postgres.connect("newsletter")
        try:
            row = connection.execute("select pg_try_advisory_lock(?::bigint)", (RUN_LOCK_KEY,)).fetchone()
            # Do not sit idle in a transaction; the session lock outlives it.
            connection.commit()
            if not row or not row[0]:
                raise RunLockBusy("Another run is in progress")
            yield
        finally:
            connection.close()
        return
    lock_path = sqlite_path.with_name(f"{sqlite_path.name}.run.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunLockBusy("Another run is in progress") from None
        yield


class Store:
    def __init__(self, path: Path):
        self.path = path
        # None until a message_log write shows whether the attempts column exists.
        self._has_attempts: bool | None = None
        if _postgres.configured():
            self.connection = _postgres.connect("newsletter")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(f"pragma busy_timeout = {BUSY_TIMEOUT_MS}")
        # Preserve the deployed backup-compatible mode: the backup container
        # opens this database through a read-only mount, where WAL readers may
        # be unable to create shared-memory sidecars.
        self.connection.execute("pragma journal_mode = delete")
        self._migrate()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        # The server opens a connection per request, so skip the DDL entirely once
        # this database is already at the current schema version.
        version = int(self.connection.execute("pragma user_version").fetchone()[0])
        if version >= SCHEMA_VERSION:
            return
        self.connection.execute(
            """
            create table if not exists feed_items (
              id text primary key,
              title text not null,
              url text not null,
              recommendation text not null,
              content_access text not null default 'full',
              short_summary text not null default '',
              why_this_matters text not null,
              novel_points text not null,
              highlights text not null,
              source_newsletter text not null,
              source_gmail_id text not null default '',
              source_subject text not null,
              source_sender text not null,
              source_gmail_url text not null,
              published_at text not null,
              email_received_at text not null,
              authors text not null default ''
            )
            """
        )
        columns = {
            row["name"]
            for row in self.connection.execute("pragma table_info(feed_items)").fetchall()
        }
        if "source_gmail_id" not in columns:
            self.connection.execute("alter table feed_items add column source_gmail_id text not null default ''")
        if "content_access" not in columns:
            self.connection.execute(
                "alter table feed_items add column content_access text not null default 'full'"
            )
        if "short_summary" not in columns:
            self.connection.execute("alter table feed_items add column short_summary text not null default ''")
            self.connection.execute(
                "update feed_items set short_summary = why_this_matters where short_summary = ''"
            )
        if "authors" not in columns:
            self.connection.execute("alter table feed_items add column authors text not null default ''")
        self.connection.execute(
            """
            create table if not exists settings (
              key text primary key,
              value text not null
            )
            """
        )
        self.connection.execute(
            "create index if not exists feed_items_source_gmail_id on feed_items(source_gmail_id)"
        )
        self.connection.execute(
            "create index if not exists feed_items_published_at on feed_items(published_at desc)"
        )
        self.connection.execute(
            "create index if not exists feed_items_email_received_at on feed_items(email_received_at)"
        )
        self.connection.execute(
            """
            create table if not exists full_items (
              gmail_id text primary key,
              title text not null,
              url text not null default '',
              publication text not null default '',
              authors text not null default '',
              content_access text not null default 'full',
              source_gmail_url text not null default '',
              bullets text not null default '',
              content_html text not null,
              email_received_at text not null,
              added_at text not null
            )
            """
        )
        self.connection.execute(
            """
            create table if not exists message_log (
              gmail_id text primary key,
              subject text not null default '',
              sender text not null default '',
              gmail_url text not null default '',
              received_at text not null default '',
              status text not null,
              detail text not null default '',
              first_seen_at text not null,
              updated_at text not null,
              attempts integer not null default 0
            )
            """
        )
        # Mirrors deploy/sql/2026-11-23-message-attempts.sql.
        log_columns = {
            row["name"] for row in self.connection.execute("pragma table_info(message_log)").fetchall()
        }
        if "attempts" not in log_columns:
            self.connection.execute("alter table message_log add column attempts integer not null default 0")
        # Mirrors deploy/sql/2026-11-24-email-content.sql.
        self.connection.execute(
            """
            create table if not exists email_content (
              gmail_id text primary key,
              subject text not null default '',
              sender text not null default '',
              received_at text not null default '',
              raw_html text not null default '',
              raw_text text not null default '',
              content_html text not null default '',
              sanitizer_version integer not null default 0,
              word_count integer not null default 0,
              stored_at text not null
            )
            """
        )
        # Mirrors deploy/sql/2026-11-25-web-links.sql.
        full_columns = {
            row["name"] for row in self.connection.execute("pragma table_info(full_items)").fetchall()
        }
        if "source_kind" not in full_columns:
            self.connection.execute("alter table full_items add column source_kind text not null default 'gmail'")
        if "source_url" not in full_columns:
            self.connection.execute("alter table full_items add column source_url text not null default ''")
        self.connection.execute("create index if not exists full_items_added_at on full_items(added_at desc)")
        self.connection.execute("create index if not exists full_items_source_url on full_items(source_url)")
        self.connection.execute(
            "create index if not exists message_log_received_at on message_log(received_at desc)"
        )
        self.connection.execute(f"pragma user_version = {SCHEMA_VERSION}")
        self.connection.commit()

    def get_setting(self, key: str) -> str:
        row = self.connection.execute("select value from settings where key = ?", (key,)).fetchone()
        return row["value"] if row else ""

    def set_setting(self, key: str, value: str) -> None:
        self.connection.execute(
            "insert into settings(key, value) values(?, ?) on conflict(key) do update set value = excluded.value",
            (key, value),
        )
        self.connection.commit()

    def message_has_items(self, gmail_id: str) -> bool:
        row = self.connection.execute(
            "select 1 from feed_items where source_gmail_id = ? limit 1", (gmail_id,)
        ).fetchone()
        return row is not None

    def stored_gmail_ids(self) -> set[str]:
        rows = self.connection.execute(
            "select distinct source_gmail_id from feed_items where source_gmail_id != ''"
        ).fetchall()
        return {row["source_gmail_id"] for row in rows}

    def add_summary(self, message: NewsletterMessage, item: SummaryItem) -> bool:
        """Store a summary; False when this email already has one.

        The insert itself ignores a duplicate, so two overlapping runs cannot
        fail on the primary key.
        """
        cursor = self._insert_summary(message, item, datetime.now(UTC).isoformat())
        self.connection.commit()
        return bool(cursor.rowcount)

    def _insert_summary(self, message: NewsletterMessage, item: SummaryItem, published_at: str):
        item_id = make_item_id(message.gmail_id)
        # Bullets are stored one per line in highlights. recommendation,
        # short_summary, why_this_matters, and novel_points belong to retired
        # feed formats and are left empty.
        return self.connection.execute(
            """
            insert into feed_items (
              id, title, url, recommendation, content_access, short_summary, why_this_matters, novel_points, highlights,
              source_newsletter, source_gmail_id, source_subject, source_sender, source_gmail_url,
              published_at, email_received_at, authors
            ) values (?, ?, ?, '', ?, '', '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do nothing
            """,
            (
                item_id,
                item.title,
                item.url,
                item.content_access,
                "\n".join(item.bullets),
                item.publication,
                message.gmail_id,
                message.subject,
                message.sender,
                message.gmail_url,
                published_at,
                message.received_at.isoformat(),
                "\n".join(item.authors),
            ),
        )

    def list_summaries(self) -> list[NewsletterSummary]:
        """Return one summary per newsletter email, oldest email first."""
        rows = self.connection.execute("select * from feed_items order by published_at").fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["source_gmail_id"] or row["id"], []).append(row)
        counts = self._word_counts()
        summaries = [
            _rows_to_summary(group, counts.get(group[0]["source_gmail_id"], 0)) for group in grouped.values()
        ]
        return sorted(summaries, key=lambda summary: summary.email_received_at)

    def get_summary_by_gmail_id(self, gmail_id: str) -> NewsletterSummary | None:
        rows = self.connection.execute(
            "select * from feed_items where source_gmail_id = ? order by published_at", (gmail_id,)
        ).fetchall()
        if not rows:
            return None
        return _rows_to_summary(rows, self._word_counts().get(gmail_id, 0))

    def replace_summary(self, message: NewsletterMessage, item: SummaryItem) -> bool:
        """Replace an email's summary rows with a new summary, in one transaction.

        The new row keeps the earliest published_at of the rows it replaces,
        so a re-summarized email stays in the digest it was published in
        instead of moving to the next one as "late". False when the email had
        no summary (nothing is written).
        """
        row = self.connection.execute(
            "select min(published_at) as published_at from feed_items where source_gmail_id = ?",
            (message.gmail_id,),
        ).fetchone()
        published_at = row["published_at"] if row else None
        if not published_at:
            return False
        try:
            self.connection.execute("delete from feed_items where source_gmail_id = ?", (message.gmail_id,))
            self._insert_summary(message, item, published_at)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return True

    # full_items and message_log need a manual PostgreSQL migration
    # (deploy/sql/2026-11-full-text.sql), and message_log.attempts another
    # (deploy/sql/2026-11-23-message-attempts.sql). Until they are applied,
    # these methods raise StoreFeatureUnavailable after rolling back, so the
    # connection stays usable for the digest. Any other error is re-raised.
    @contextmanager
    def _new_tables(self, migration: str = FULL_TEXT_MIGRATION) -> Iterator[None]:
        try:
            yield
        except Exception as exc:
            self.connection.rollback()
            if is_missing_schema_error(exc):
                raise StoreFeatureUnavailable(f"A table or column is missing; apply {migration}") from exc
            raise

    def add_full_item(self, item: FullItem) -> None:
        """Insert or refresh an item; re-adding keeps its original added_at.

        Web items also write source_kind and source_url, so they need
        WEB_MIGRATION; email items work without it.
        """
        web = item.is_web
        extra_columns = ", source_kind, source_url" if web else ""
        extra_values = ", ?, ?" if web else ""
        with self._new_tables(WEB_MIGRATION if web else FULL_TEXT_MIGRATION):
            self.connection.execute(
                f"""
                insert into full_items (
                  gmail_id, title, url, publication, authors, content_access, source_gmail_url,
                  bullets, content_html, email_received_at, added_at{extra_columns}
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?{extra_values})
                on conflict(gmail_id) do update set
                  title = excluded.title,
                  url = excluded.url,
                  publication = excluded.publication,
                  authors = excluded.authors,
                  content_access = excluded.content_access,
                  source_gmail_url = excluded.source_gmail_url,
                  bullets = excluded.bullets,
                  content_html = excluded.content_html,
                  email_received_at = excluded.email_received_at
                """,
                (
                    item.gmail_id,
                    item.title,
                    item.url,
                    item.publication,
                    "\n".join(item.authors),
                    item.content_access,
                    item.source_gmail_url,
                    "\n".join(item.bullets),
                    item.content_html,
                    item.email_received_at,
                    item.added_at,
                    *((item.source_kind, item.source_url) if web else ()),
                ),
            )
            self.connection.commit()

    def web_items_available(self) -> bool:
        """Whether web pages can be saved (WEB_MIGRATION is applied)."""
        try:
            with self._new_tables(WEB_MIGRATION):
                self.connection.execute("select source_kind, source_url from full_items where 1 = 0").fetchall()
                self.connection.commit()
        except StoreFeatureUnavailable:
            return False
        return True

    def find_web_item(self, item_id: str, source_url: str = "") -> FullItem | None:
        """A saved web page by its ID, or by the URL it was added from."""
        item = self.get_full_item(item_id)
        if item is not None or not source_url:
            return item
        try:
            with self._new_tables(WEB_MIGRATION):
                row = self.connection.execute(
                    "select * from full_items where source_url = ? order by added_at desc limit 1", (source_url,)
                ).fetchone()
        except StoreFeatureUnavailable:
            return None
        return _row_to_full_item(row) if row else None

    def update_full_item_summary(
        self, item_id: str, *, bullets: list[str], content_access: str, authors: list[str] | None = None
    ) -> bool:
        """Set an item's key points after it was saved (web pages are summarized in the background)."""
        with self._new_tables():
            if authors is None:
                cursor = self.connection.execute(
                    "update full_items set bullets = ?, content_access = ? where gmail_id = ?",
                    ("\n".join(bullets), content_access, item_id),
                )
            else:
                cursor = self.connection.execute(
                    "update full_items set bullets = ?, content_access = ?, authors = ? where gmail_id = ?",
                    ("\n".join(bullets), content_access, "\n".join(authors), item_id),
                )
            self.connection.commit()
        return bool(cursor.rowcount)

    def get_full_item(self, gmail_id: str) -> FullItem | None:
        with self._new_tables():
            row = self.connection.execute("select * from full_items where gmail_id = ?", (gmail_id,)).fetchone()
        return _row_to_full_item(row) if row else None

    def list_full_items(self, limit: int) -> list[FullItem]:
        with self._new_tables():
            rows = self.connection.execute(
                "select * from full_items order by added_at desc, gmail_id limit ?", (limit,)
            ).fetchall()
        return [_row_to_full_item(row) for row in rows]

    def full_item_ids(self) -> set[str]:
        with self._new_tables():
            rows = self.connection.execute("select gmail_id from full_items").fetchall()
        return {row["gmail_id"] for row in rows}

    def delete_full_item(self, gmail_id: str) -> bool:
        with self._new_tables():
            cursor = self.connection.execute("delete from full_items where gmail_id = ?", (gmail_id,))
            self.connection.commit()
        return bool(cursor.rowcount)

    def log_message(
        self,
        gmail_id: str,
        *,
        status: str,
        subject: str = "",
        sender: str = "",
        gmail_url: str = "",
        received_at: str = "",
        detail: str = "",
        count_attempt: bool = False,
    ) -> None:
        """Record what `run` did with a message; blank fields keep earlier values.

        count_attempt adds one to the message's summary attempt count. Without
        the attempts column (an unmigrated PostgreSQL database), the row is
        still written and attempts are not counted.
        """
        if status not in MESSAGE_STATUSES:
            raise ValueError(f"Unknown message status {status!r}")
        now = _utc_now()
        values = [gmail_id, subject, sender, gmail_url, received_at, status, detail[:1000], now, now]
        upsert = """
                on conflict(gmail_id) do update set
                  subject = case when excluded.subject = '' then message_log.subject else excluded.subject end,
                  sender = case when excluded.sender = '' then message_log.sender else excluded.sender end,
                  gmail_url = case when excluded.gmail_url = '' then message_log.gmail_url else excluded.gmail_url end,
                  received_at = case when excluded.received_at = '' then message_log.received_at
                                     else excluded.received_at end,
                  status = excluded.status,
                  detail = excluded.detail,
                  updated_at = excluded.updated_at"""
        columns = "gmail_id, subject, sender, gmail_url, received_at, status, detail, first_seen_at, updated_at"
        if self._has_attempts is not False:
            try:
                with self._new_tables(ATTEMPTS_MIGRATION):
                    self.connection.execute(
                        f"insert into message_log ({columns}, attempts) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        f"{upsert}, attempts = message_log.attempts + excluded.attempts",
                        (*values, 1 if count_attempt else 0),
                    )
                    self.connection.commit()
                self._has_attempts = True
                return
            except StoreFeatureUnavailable:
                self._has_attempts = False
                logging.warning("message_log.attempts is missing; apply %s to count summary attempts", ATTEMPTS_MIGRATION)
        with self._new_tables():
            self.connection.execute(
                f"insert into message_log ({columns}) values (?, ?, ?, ?, ?, ?, ?, ?, ?){upsert}", values
            )
            self.connection.commit()

    def gave_up_ids(self, max_attempts: int) -> set[str]:
        """Emails whose summary failed max_attempts times; `run` stops retrying them."""
        with self._new_tables(ATTEMPTS_MIGRATION):
            rows = self.connection.execute(
                "select gmail_id from message_log where status = 'summary_failed' and attempts >= ?",
                (max_attempts,),
            ).fetchall()
        return {row["gmail_id"] for row in rows}

    def list_unsummarized(self) -> list[dict]:
        """Skipped or failed emails that have no summary, for the digest's "Not summarized" section."""
        placeholders = ", ".join("?" for _ in UNSUMMARIZED_STATUSES)
        with self._new_tables():
            rows = self.connection.execute(
                f"""
                select gmail_id, subject, sender, received_at, status, detail, first_seen_at
                  from message_log m
                 where status in ({placeholders})
                   and not exists (select 1 from feed_items f where f.source_gmail_id = m.gmail_id)
                """,
                UNSUMMARIZED_STATUSES,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_messages(self, limit: int, offset: int = 0) -> list[dict]:
        """Every email the service has handled, newest first.

        Summaries from before message_log existed are included as summarized.
        Without message_log (an unmigrated PostgreSQL database), only those
        summaries are listed.
        """
        history = """
            select distinct source_gmail_id as gmail_id, source_subject as subject, source_sender as sender,
                   source_gmail_url as gmail_url, email_received_at as received_at,
                   'summarized' as status, '' as detail
              from feed_items f
             where source_gmail_id != ''
        """
        try:
            with self._new_tables():
                rows = self.connection.execute(
                    f"""
                    select gmail_id, subject, sender, gmail_url, received_at, status, detail from message_log
                    union all
                    {history}
                       and not exists (select 1 from message_log m where m.gmail_id = f.source_gmail_id)
                    order by received_at desc, gmail_id
                    limit ? offset ?
                    """,
                    (limit, offset),
                ).fetchall()
        except StoreFeatureUnavailable:
            logging.warning("message_log is unavailable; listing summarized emails only")
            rows = self.connection.execute(
                f"{history} order by received_at desc, gmail_id limit ? offset ?", (limit, offset)
            ).fetchall()
        return [dict(row) for row in rows]

    def is_known_gmail_id(self, gmail_id: str) -> bool:
        """True only for emails this service has already processed."""
        if self.message_has_items(gmail_id):
            return True
        try:
            with self._new_tables():
                row = self.connection.execute(
                    "select 1 from message_log where gmail_id = ?", (gmail_id,)
                ).fetchone()
        except StoreFeatureUnavailable:
            return False
        return row is not None

    # email_content needs deploy/sql/2026-11-24-email-content.sql on
    # PostgreSQL; these raise StoreFeatureUnavailable until it is applied.

    def save_content(self, content: StoredContent) -> None:
        """Insert or replace an email's stored content."""
        with self._new_tables(CONTENT_MIGRATION):
            self.connection.execute(
                """
                insert into email_content (
                  gmail_id, subject, sender, received_at, raw_html, raw_text, content_html,
                  sanitizer_version, word_count, stored_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(gmail_id) do update set
                  subject = excluded.subject,
                  sender = excluded.sender,
                  received_at = excluded.received_at,
                  raw_html = excluded.raw_html,
                  raw_text = excluded.raw_text,
                  content_html = excluded.content_html,
                  sanitizer_version = excluded.sanitizer_version,
                  word_count = excluded.word_count,
                  stored_at = excluded.stored_at
                """,
                (
                    content.gmail_id,
                    content.subject,
                    content.sender,
                    content.received_at,
                    content.raw_html,
                    content.raw_text,
                    content.content_html,
                    content.sanitizer_version,
                    content.word_count,
                    content.stored_at or _utc_now(),
                ),
            )
            self.connection.commit()

    def delete_content(self, gmail_id: str) -> None:
        with self._new_tables(CONTENT_MIGRATION):
            self.connection.execute("delete from email_content where gmail_id = ?", (gmail_id,))
            self.connection.commit()

    def get_content(self, gmail_id: str) -> StoredContent | None:
        with self._new_tables(CONTENT_MIGRATION):
            row = self.connection.execute("select * from email_content where gmail_id = ?", (gmail_id,)).fetchone()
        return _row_to_content(row) if row else None

    def update_sanitized(self, gmail_id: str, *, content_html: str, sanitizer_version: int, word_count: int) -> None:
        with self._new_tables(CONTENT_MIGRATION):
            self.connection.execute(
                "update email_content set content_html = ?, sanitizer_version = ?, word_count = ? where gmail_id = ?",
                (content_html, sanitizer_version, word_count, gmail_id),
            )
            self.connection.commit()

    def stale_content_ids(self, sanitizer_version: int) -> list[str]:
        """Stored emails sanitized by an older sanitizer that still have their raw body."""
        with self._new_tables(CONTENT_MIGRATION):
            rows = self.connection.execute(
                """
                select gmail_id from email_content
                 where sanitizer_version < ? and (raw_html != '' or raw_text != '')
                 order by received_at
                """,
                (sanitizer_version,),
            ).fetchall()
        return [row["gmail_id"] for row in rows]

    def content_ids(self) -> list[str]:
        with self._new_tables(CONTENT_MIGRATION):
            rows = self.connection.execute("select gmail_id from email_content order by received_at").fetchall()
        return [row["gmail_id"] for row in rows]

    def _word_counts(self) -> dict[str, int]:
        """gmail_id -> word count of the stored content; empty before the migration."""
        try:
            with self._new_tables(CONTENT_MIGRATION):
                rows = self.connection.execute(
                    "select gmail_id, word_count from email_content where word_count > 0"
                ).fetchall()
        except StoreFeatureUnavailable:
            return {}
        return {row["gmail_id"]: int(row["word_count"]) for row in rows}

    def migration_state(self) -> list[tuple[str, str, bool]]:
        """(feature, migration file, installed) for each optional PostgreSQL migration."""
        state = []
        for feature, table, column, migration in MIGRATIONS:
            try:
                with self._new_tables(migration):
                    self.connection.execute(f"select {column} from {table} where 1 = 0").fetchall()
                    # Leave no read transaction open on PostgreSQL.
                    self.connection.commit()
                state.append((feature, migration, True))
            except StoreFeatureUnavailable:
                state.append((feature, migration, False))
        return state

    def recent_problems(self, since: str, max_attempts: int) -> list[dict]:
        """Emails whose fetch or summary failed since `since`, newest first, with attempts when known."""
        try:
            with self._new_tables(ATTEMPTS_MIGRATION):
                rows = self.connection.execute(
                    """
                    select gmail_id, subject, sender, received_at, status, detail, updated_at, attempts
                      from message_log
                     where status in ('summary_failed', 'fetch_failed') and updated_at >= ?
                     order by updated_at desc
                    """,
                    (since,),
                ).fetchall()
        except StoreFeatureUnavailable:
            with self._new_tables():
                rows = self.connection.execute(
                    """
                    select gmail_id, subject, sender, received_at, status, detail, updated_at, 0 as attempts
                      from message_log
                     where status in ('summary_failed', 'fetch_failed') and updated_at >= ?
                     order by updated_at desc
                    """,
                    (since,),
                ).fetchall()
        problems = [dict(row) for row in rows]
        for problem in problems:
            problem["gave_up"] = problem["status"] == "summary_failed" and int(problem["attempts"]) >= max_attempts
        return problems


def make_item_id(gmail_id: str) -> str:
    digest = hashlib.sha256(gmail_id.encode("utf-8")).hexdigest()[:24]
    return f"newsletter-summary:{digest}"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _lines(value: str) -> list[str]:
    return [line for line in value.splitlines() if line]


def _row_to_full_item(row) -> FullItem:
    return FullItem(
        gmail_id=row["gmail_id"],
        title=row["title"],
        url=strip_credentials(row["url"]),
        publication=row["publication"],
        authors=_lines(row["authors"]),
        content_access=row["content_access"],
        source_gmail_url=unify_gmail_link(row["source_gmail_url"]),
        bullets=_lines(row["bullets"]),
        content_html=row["content_html"],
        email_received_at=row["email_received_at"],
        added_at=row["added_at"],
        # Absent until WEB_MIGRATION is applied.
        source_kind=_optional(row, "source_kind", "gmail") or "gmail",
        source_url=_optional(row, "source_url", ""),
    )


def _optional(row, key: str, default: str) -> str:
    return row[key] if key in row.keys() else default


def _row_to_content(row) -> StoredContent:
    return StoredContent(
        gmail_id=row["gmail_id"],
        subject=row["subject"],
        sender=row["sender"],
        received_at=row["received_at"],
        raw_html=row["raw_html"],
        raw_text=row["raw_text"],
        content_html=row["content_html"],
        sanitizer_version=int(row["sanitizer_version"]),
        word_count=int(row["word_count"]),
        stored_at=row["stored_at"],
    )


def _rows_to_summary(rows: list[sqlite3.Row], word_count: int = 0) -> NewsletterSummary:
    row = rows[0]
    title = row["title"]
    url = unify_gmail_link(strip_credentials(row["url"]))
    gmail_url = unify_gmail_link(row["source_gmail_url"])
    if len(rows) > 1:
        # Older versions stored one row per linked story; present those emails
        # as the newsletter issue they came from.
        title = row["source_subject"] or title
        url = gmail_url or url
    paragraph = row["short_summary"] or row["why_this_matters"]
    # Rows from before bullets hold a paragraph plus a retired "highlights"
    # format; show the paragraph for those.
    legacy = len(rows) > 1 or bool(paragraph)
    return NewsletterSummary(
        id=row["id"],
        title=title,
        url=url,
        publication=row["source_newsletter"],
        authors=_lines(row["authors"]),
        bullets=[] if legacy else _lines(row["highlights"]),
        summary=paragraph,
        content_access=row["content_access"],
        source_gmail_id=row["source_gmail_id"],
        source_subject=row["source_subject"],
        source_sender=row["source_sender"],
        source_gmail_url=gmail_url,
        email_received_at=datetime.fromisoformat(row["email_received_at"]),
        created_at=_parse_time(row["published_at"]),
        word_count=word_count,
    )


def _parse_time(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
