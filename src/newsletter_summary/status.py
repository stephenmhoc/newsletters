"""What the private /status page shows: runs, failures, FreshRSS pings, migrations."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import AppConfig
from .digest import PUBLICATION_KEY, Publication
from .run_state import RunAlert, RunState, failing_since, run_alert
from .store import Store

REFRESH_KEY = "freshrss_refresh"
# Runs are hourly; warn when none has succeeded for this long.
STATUS_STALE_AFTER = timedelta(hours=2)
PROBLEM_LOOKBACK = timedelta(days=14)
STUCK_AFTER = timedelta(hours=1)


def record_refresh(store: Store, *, ok: bool, detail: str, now: datetime | None = None) -> None:
    """Remember the latest FreshRSS refresh ping. detail never holds the URL (it carries a token)."""
    moment = (now or datetime.now(UTC)).replace(microsecond=0)
    store.set_setting(REFRESH_KEY, json.dumps({"at": moment.isoformat(), "ok": ok, "detail": detail[:200]}))


def last_refresh(store: Store) -> dict | None:
    raw = store.get_setting(REFRESH_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


@dataclass
class Status:
    run: dict
    alert: RunAlert | None
    problems: list[dict]
    refresh: dict | None
    migrations: list[tuple[str, str, bool]]
    last_published_day: str = ""
    warnings: list[str] = field(default_factory=list)


def gather_status(store: Store, config: AppConfig, *, now: datetime, max_attempts: int) -> Status:
    run = RunState(config).data
    migrations = store.migration_state()
    has_log = any(installed for feature, _, installed in migrations if feature == "Email log")
    since = (now - PROBLEM_LOOKBACK).astimezone(UTC).isoformat()
    problems = store.recent_problems(since, max_attempts) if has_log else []
    try:
        publication = Publication.loads(store.get_setting(PUBLICATION_KEY))
    except (ValueError, KeyError, TypeError):
        logging.exception("Unreadable digest publication record")
        publication = None
    status = Status(
        run=run,
        alert=run_alert(run, now=now, timezone=config.gmail.tzinfo),
        problems=problems,
        refresh=last_refresh(store),
        migrations=migrations,
        last_published_day=max(publication.days).isoformat() if publication and publication.days else "",
    )
    status.warnings = _warnings(status, now)
    return status


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _warnings(status: Status, now: datetime) -> list[str]:
    warnings = []
    run = status.run
    since = failing_since(run)
    if since is not None:
        warnings.append(f"Runs have been failing since {since.isoformat()}.")
    last_success = _parse(run.get("last_success"))
    if last_success is None:
        warnings.append("No run has ever succeeded.")
    elif now - last_success > STATUS_STALE_AFTER:
        hours = (now - last_success).total_seconds() / 3600
        warnings.append(f"No run has succeeded for {hours:.1f} hours; runs are hourly.")
    started = _parse(run.get("started_at"))
    if run.get("status") == "running" and started is not None and now - started > STUCK_AFTER:
        warnings.append("A run has been marked running for over an hour; it may have been killed.")
    gave_up = [problem for problem in status.problems if problem.get("gave_up")]
    if gave_up:
        warnings.append(f"{len(gave_up)} email(s) were given up on after repeated summary failures.")
    if status.refresh is not None and not status.refresh.get("ok"):
        warnings.append("The last FreshRSS refresh ping failed.")
    missing = [migration for _, migration, installed in status.migrations if not installed]
    if missing:
        warnings.append("Migrations not applied: " + ", ".join(sorted(set(missing))) + ".")
    return warnings
