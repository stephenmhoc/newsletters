"""Group summaries into daily digests.

A digest dated D covers the digest hour on D-1 until the digest hour on D. It
is *published* when the run that closes its window finishes: `run` records
that moment per day (see Publication), and the feed and the web UI show only
published days. Recording it makes two things exact:

- A summary created after its day was published (a retry, a manual run, the
  four-hour overlap) is not slipped into an entry readers already saw. It
  moves to the next digest published after it was created, under
  "Late from <day>".
- Skipped or failed emails are listed under "Not summarized" in the digest of
  the day they arrived.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parseaddr
from zoneinfo import ZoneInfo

from .config import AppConfig
from .store import NewsletterSummary, Store, StoreFeatureUnavailable

PUBLICATION_KEY = "digest_publication"


@dataclass(frozen=True)
class MissedMessage:
    """An email that is not in any digest because it was skipped or its summary failed."""

    gmail_id: str
    subject: str
    sender: str
    status: str
    detail: str
    # When Gmail received it; first_seen_at stands in when it is unknown (fetch failures).
    received_at: datetime


@dataclass(frozen=True)
class DailyDigest:
    """Every newsletter received in the day ending at the digest hour."""

    day: date
    start: datetime
    end: datetime
    newsletters: list[NewsletterSummary]
    # Summaries for earlier, already published days, oldest day first.
    late: list[tuple[date, list[NewsletterSummary]]] = field(default_factory=list)
    not_summarized: list[MissedMessage] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"newsletter-digest:{self.day.isoformat()}"

    @property
    def summary_count(self) -> int:
        return len(self.newsletters) + sum(len(items) for _, items in self.late)


@dataclass
class Publication:
    """When each digest day was published, kept in the settings table.

    Days whose window closed before `since` (when this record started) were
    published by older versions: they are shown, and nothing counts as late
    for them.
    """

    since: datetime
    days: dict[date, datetime] = field(default_factory=dict)

    @classmethod
    def loads(cls, raw: str) -> Publication | None:
        if not raw:
            return None
        data = json.loads(raw)
        return cls(
            since=datetime.fromisoformat(data["since"]),
            days={date.fromisoformat(day): datetime.fromisoformat(moment) for day, moment in data["days"].items()},
        )

    def dumps(self) -> str:
        return json.dumps(
            {
                "since": self.since.isoformat(),
                "days": {day.isoformat(): moment.isoformat() for day, moment in sorted(self.days.items())},
            }
        )

    def visible(self, day: date, end: datetime) -> bool:
        return day in self.days or end <= self.since

    def host_for(self, day: date, created_at: datetime | None) -> date | None:
        """Where a summary for `day` belongs: `day`, a later published day, or None while it waits.

        It is late when it was created after `day` was published; it then
        goes to the first later day published after it was created.
        """
        published = self.days.get(day)
        if published is None or created_at is None or created_at <= published:
            return day
        later = [other for other, moment in self.days.items() if other > day and moment >= created_at]
        return min(later) if later else None


def digest_window(day: date, timezone: ZoneInfo, hour: int) -> tuple[datetime, datetime]:
    """Return [hour on the prior day, hour on this day) in local time."""
    end = datetime.combine(day, time(hour), tzinfo=timezone)
    start = datetime.combine(day - timedelta(days=1), time(hour), tzinfo=timezone)
    return start, end


def digest_day(moment: datetime, timezone: ZoneInfo, hour: int) -> date:
    local = moment.astimezone(timezone)
    if local.hour >= hour:
        return local.date() + timedelta(days=1)
    return local.date()


def missed_messages(rows: list[dict]) -> list[MissedMessage]:
    """MissedMessage objects from Store.list_unsummarized() rows."""
    missed = []
    for row in rows:
        moment = _parse(str(row.get("received_at") or "")) or _parse(str(row.get("first_seen_at") or ""))
        if moment is None:
            continue
        missed.append(
            MissedMessage(
                gmail_id=str(row["gmail_id"]),
                subject=str(row.get("subject") or ""),
                sender=str(row.get("sender") or ""),
                status=str(row.get("status") or ""),
                detail=str(row.get("detail") or ""),
                received_at=moment,
            )
        )
    return missed


def build_digests(
    summaries: list[NewsletterSummary],
    *,
    timezone: ZoneInfo,
    hour: int,
    now: datetime,
    max_digests: int | None = None,
    missed: list[MissedMessage] | None = None,
    publication: Publication | None = None,
) -> list[DailyDigest]:
    """Completed daily digests, newest first.

    Without a publication record (before the first run of this version) every
    closed window is shown and nothing is late, as before. Days with nothing
    to show produce no entry.
    """
    on_time: dict[date, list[NewsletterSummary]] = {}
    late: dict[date, dict[date, list[NewsletterSummary]]] = {}
    for summary in summaries:
        day = digest_day(summary.email_received_at, timezone, hour)
        host = publication.host_for(day, summary.created_at) if publication else day
        if host is None:
            continue
        if host == day:
            on_time.setdefault(day, []).append(summary)
        else:
            late.setdefault(host, {}).setdefault(day, []).append(summary)
    not_summarized: dict[date, list[MissedMessage]] = {}
    for message in missed or []:
        not_summarized.setdefault(digest_day(message.received_at, timezone, hour), []).append(message)

    digests = []
    for day in sorted({*on_time, *late, *not_summarized}, reverse=True):
        start, end = digest_window(day, timezone, hour)
        if end > now or (publication is not None and not publication.visible(day, end)):
            continue
        digests.append(
            DailyDigest(
                day=day,
                start=start,
                end=end,
                newsletters=sorted(on_time.get(day, []), key=lambda summary: summary.email_received_at),
                late=[
                    (late_day, sorted(items, key=lambda summary: summary.email_received_at))
                    for late_day, items in sorted(late.get(day, {}).items())
                ],
                not_summarized=sorted(not_summarized.get(day, []), key=lambda message: message.received_at),
            )
        )
    if max_digests is not None:
        return digests[:max_digests]
    return digests


def publish(
    summaries: list[NewsletterSummary],
    *,
    timezone: ZoneInfo,
    hour: int,
    now: datetime,
    missed: list[MissedMessage] | None = None,
    publication: Publication | None,
) -> Publication:
    """Record which digest days are published as of `now`; called at the end of each `run`.

    Every closed day with something to show is published now unless it was
    already. A late summary with nowhere to go is published in the newest
    closed day, unless that day was published before the summary existed; it
    then waits for the next day's digest.
    """
    result = publication or Publication(since=now.astimezone(UTC))
    newest_closed = digest_day(now, timezone, hour) - timedelta(days=1)
    for summary in summaries:
        day = digest_day(summary.email_received_at, timezone, hour)
        _, end = digest_window(day, timezone, hour)
        if end > now or not _tracked(result, day, end):
            continue
        if result.host_for(day, summary.created_at) is None and newest_closed > day:
            result.days.setdefault(newest_closed, now)
        elif day not in result.days:
            result.days[day] = now
    for message in missed or []:
        day = digest_day(message.received_at, timezone, hour)
        _, end = digest_window(day, timezone, hour)
        if end <= now and _tracked(result, day, end):
            result.days.setdefault(day, now)
    return result


def _tracked(publication: Publication, day: date, end: datetime) -> bool:
    """Days older than the record were published by an earlier version."""
    return day in publication.days or end > publication.since


def _parse(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _publication_key(name: str) -> str:
    """Case, punctuation, and a leading "The" do not make a different name."""
    words = re.sub(r"[^\w\s]", " ", name.casefold()).split()
    if words[:1] == ["the"]:
        words = words[1:]
    return " ".join(words)


def _variants(a: str, b: str) -> bool:
    """"The Backstory" and "The Backstory (Puck and Air Mail)" name one publication."""
    key_a, key_b = _publication_key(a), _publication_key(b)
    if not key_a or not key_b:
        return False
    return key_a == key_b or key_a.startswith(key_b + " ") or key_b.startswith(key_a + " ")


def canonical_publications(
    summaries: list[NewsletterSummary], overrides: Mapping[str, str] | None = None
) -> list[NewsletterSummary]:
    """Give each sender a stable publication name.

    The model names the publication per email, so one sender can show up as
    "The Backstory" one day and "The Backstory (Puck and Air Mail)" the next.
    For each sender address, a name becomes the most common of its variants
    (ties go to the most recent). Names that are not variants of each other
    are left alone, since one address can send several distinct newsletters.
    `overrides` (rss.publication_names: sender address -> name) wins outright.
    """
    overrides = {address.lower(): name for address, name in (overrides or {}).items()}
    counts: dict[str, dict[str, tuple[int, datetime]]] = {}
    for summary in summaries:
        address = parseaddr(summary.source_sender)[1].lower()
        if not address or not summary.publication:
            continue
        names = counts.setdefault(address, {})
        count, latest = names.get(summary.publication, (0, summary.email_received_at))
        names[summary.publication] = (count + 1, max(latest, summary.email_received_at))

    def canonical(address: str, name: str) -> str:
        if address in overrides:
            return overrides[address]
        names = counts.get(address, {})
        candidates = [other for other in names if other == name or _variants(other, name)]
        if not candidates:
            return name
        return max(candidates, key=lambda other: names[other])

    result = []
    for summary in summaries:
        address = parseaddr(summary.source_sender)[1].lower()
        name = canonical(address, summary.publication) if address else summary.publication
        result.append(summary if name == summary.publication else replace(summary, publication=name))
    return result


def load_summaries(store: Store, config: AppConfig) -> list[NewsletterSummary]:
    """Every summary, oldest email first, with stable publication names."""
    return canonical_publications(store.list_summaries(), config.rss.publication_names)


def open_window(config: AppConfig, now: datetime) -> tuple[date, datetime, datetime]:
    """The digest still collecting mail: (its day, window start, window end)."""
    day = digest_day(now, config.gmail.tzinfo, config.rss.digest_hour)
    start, end = digest_window(day, config.gmail.tzinfo, config.rss.digest_hour)
    return day, start, end


def today_so_far(
    store: Store, config: AppConfig, *, now: datetime
) -> tuple[date, datetime, list[NewsletterSummary], list[MissedMessage]]:
    """Newsletters summarized so far for the digest that publishes at the next digest hour."""
    summaries, missed, _ = _load(store, config)
    day, start, end = open_window(config, now)
    newsletters = [summary for summary in summaries if start <= summary.email_received_at < end]
    problems = [message for message in missed if start <= message.received_at < end]
    return day, end, newsletters, sorted(problems, key=lambda message: message.received_at)


def load_digests(store: Store, config: AppConfig, *, now: datetime, max_digests: int | None = None) -> list[DailyDigest]:
    """The published digests, as the feed and the web UI show them."""
    summaries, missed, publication = _load(store, config)
    return build_digests(
        summaries,
        timezone=config.gmail.tzinfo,
        hour=config.rss.digest_hour,
        now=now,
        max_digests=max_digests,
        missed=missed,
        publication=publication,
    )


def record_publication(store: Store, config: AppConfig, *, now: datetime) -> None:
    """Publish every digest whose window has closed; `run` calls this when it finishes."""
    summaries, missed, publication = _load(store, config)
    updated = publish(
        summaries,
        timezone=config.gmail.tzinfo,
        hour=config.rss.digest_hour,
        now=now,
        missed=missed,
        publication=publication,
    )
    store.set_setting(PUBLICATION_KEY, updated.dumps())


def _load(store: Store, config: AppConfig) -> tuple[list[NewsletterSummary], list[MissedMessage], Publication | None]:
    try:
        missed = missed_messages(store.list_unsummarized())
    except StoreFeatureUnavailable:
        missed = []
    try:
        publication = Publication.loads(store.get_setting(PUBLICATION_KEY))
    except (ValueError, KeyError, TypeError):
        logging.exception("Ignoring an unreadable digest publication record")
        publication = None
    return load_summaries(store, config), missed, publication
