from __future__ import annotations

import json
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from . import _postgres
from .config import AppConfig
from .rss import digest_feed_token, web_secret
from .store import Store

STATE_KEY = "newsletter_run_state"
# Runs are hourly, but readers are only alerted when no run has succeeded for
# a day and two hours: an outage that short never delays a digest by a day.
STALE_AFTER = timedelta(hours=26)
# Hourly runs retry on their own, so a failure is only reported (alert item,
# health check) once runs have kept failing this long. One failing email is
# given up on after three hourly attempts, inside this grace period.
FAILURE_GRACE = timedelta(hours=3)


class RunState:
    """Persist progress independently of whether a run finds any new mail."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.source = {"user_id": config.gmail.user_id, "label": config.gmail.label}
        self.data = self._read()

    def _read(self) -> dict:
        path = self.config.storage.sqlite_path
        if not _postgres.configured() and not path.exists():
            return {}
        # Health checks must not create a database or run schema migrations.
        connection = (_postgres.connect("newsletter", readonly=True) if _postgres.configured() else
                      sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5))
        with closing(connection):
            row = connection.execute("select value from settings where key = ?", (STATE_KEY,)).fetchone()
        data = json.loads(row[0]) if row else {}
        if not isinstance(data, dict):
            raise ValueError("Invalid stored newsletter run state")
        return data if data.get("source") == self.source else {}

    def _save(self) -> None:
        with Store(self.config.storage.sqlite_path) as store:
            store.set_setting(STATE_KEY, json.dumps(self.data))

    def catchup_start(self, default: datetime) -> datetime:
        # Retain the full window after failures/interruption; overlap successful
        # windows by four hours, as in the original daily 28-hour lookback.
        retry = self.data.get("retry_from")
        if retry:
            return min(default, datetime.fromisoformat(retry))
        checkpoint = self.data.get("last_window_end")
        if checkpoint:
            return min(default, datetime.fromisoformat(checkpoint) - timedelta(hours=4))
        return default

    def start(self, start: datetime, end: datetime) -> None:
        self.data.update(
            source=self.source,
            status="running",
            started_at=datetime.now(UTC).isoformat(),
            window_end=end.isoformat(),
            retry_from=start.isoformat(),
        )
        self._save()

    def finish(self, *, success: bool) -> None:
        finished = datetime.now(UTC).isoformat()
        self.data.update(status="ok" if success else "failed", finished_at=finished)
        if success:
            self.data.update(
                last_success=finished, last_window_end=self.data["window_end"], retry_from=None, failing_since=None
            )
        elif not self.data.get("failing_since"):
            # The start of this streak of failed runs.
            self.data["failing_since"] = self.data.get("started_at") or finished
        self._save()


def failing_since(state: dict) -> datetime | None:
    """When the current streak of failed runs began, or None when the latest run did not fail."""
    if state.get("status") != "failed":
        return None
    return _parse(state.get("failing_since")) or _parse(state.get("finished_at"))


def failure_reportable(state: dict, now: datetime) -> bool:
    since = failing_since(state)
    return since is not None and now - since >= FAILURE_GRACE


@dataclass(frozen=True)
class RunAlert:
    """A synthetic digest feed item saying runs are failing or overdue."""

    guid: str
    title: str
    html: str
    published: datetime


def run_alert(state: dict, *, now: datetime, timezone: ZoneInfo) -> RunAlert | None:
    """An alert when runs have failed for FAILURE_GRACE, or no run succeeded within STALE_AFTER.

    Its GUID names the start of the failure streak or the last success, so
    each outage shows up in the reader as one new item, not one per run.
    """
    last_success = _parse(state.get("last_success"))
    since = failing_since(state)
    if since is not None and failure_reportable(state, now):
        when = _local(since, timezone)
        return RunAlert(
            guid=f"newsletter-digest-alert:failed:{since.isoformat()}",
            title=f"Digest runs failing since {when}",
            html=(
                f"<p>Newsletter digest runs have failed since {escape(when)}, so a digest may be "
                "missing or incomplete. Check the summarizer log and the site's status page; the next "
                "run retries the whole window.</p>"
                + _last_success_html(last_success, timezone)
            ),
            published=since,
        )
    if last_success is not None and now - last_success <= STALE_AFTER:
        return None
    if last_success is None:
        return RunAlert(
            guid="newsletter-digest-alert:stale:never",
            title="Digest is stale: no successful run recorded",
            html="<p>The newsletter digest has never completed a run. Run the summarizer once.</p>",
            published=now,
        )
    when = _local(last_success, timezone)
    return RunAlert(
        guid=f"newsletter-digest-alert:stale:{last_success.isoformat()}",
        title=f"Digest is stale: last successful run at {when}",
        html=(
            f"<p>No newsletter digest run has succeeded since {escape(when)}. Check the scheduler and the "
            "summarizer log; the next successful run catches up from where it stopped.</p>"
        ),
        published=last_success + STALE_AFTER,
    )


def _last_success_html(last_success: datetime | None, timezone: ZoneInfo) -> str:
    if last_success is None:
        return ""
    return f"<p>Last successful run: {escape(_local(last_success, timezone))}.</p>"


def _local(moment: datetime, timezone: ZoneInfo) -> str:
    local = moment.astimezone(timezone)
    return f"{local:%a %b} {local.day}, {local.hour % 12 or 12}:{local:%M %p %Z}"


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def check_health(config: AppConfig, max_age_hours: float = 30) -> int:
    """Check actual run progress and HTTP serving, with no external API calls."""
    try:
        if not 0 < max_age_hours < float("inf"):
            raise ValueError("max-age-hours must be a positive finite number")
        state = RunState(config).data
        if not state.get("last_success"):
            raise ValueError("No successful scheduled-window run recorded; run the summarizer once")
        if failure_reportable(state, datetime.now(UTC)):
            raise ValueError("Runs have been failing for over three hours; inspect the summarizer log")
        age = datetime.now(UTC) - datetime.fromisoformat(state["last_success"])
        if age < timedelta(minutes=-5) or age > timedelta(hours=max_age_hours):
            raise ValueError(f"Last successful run is {age.total_seconds() / 3600:.1f} hours old")
        secret = web_secret(config)
        if not secret:
            raise ValueError(f"${config.web.secret_env} is not set, so the digest feed is not served")
        host = _local_host(config.server.host)
        # The URL holds the feed's secret token; it is never printed.
        response = requests.get(
            f"http://{host}:{config.server.port}/digest/{digest_feed_token(secret)}/feed.xml", timeout=5
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        if root.tag != "rss" or root.find("channel") is None:
            raise ValueError("Feed server did not return an RSS document")
        if config.web.enabled:
            _check_web_listener(config)
    except (OSError, ValueError, TypeError, sqlite3.Error, requests.RequestException, ET.ParseError) as exc:
        print(f"Unhealthy: {exc}".replace(digest_feed_token(web_secret(config)), "<redacted>"))
        return 1
    web = "; private site listener is up and closed" if config.web.enabled else ""
    print(f"Healthy: feed is serving{web}; last successful run {state['last_success']}")
    return 0


def _local_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1" if host == "0.0.0.0" else "::1"
    return f"[{host}]" if ":" in host else host


def _check_web_listener(config: AppConfig) -> None:
    """The private listener must be up and refuse a request without an Access token.

    Without a token every route answers 403 from the app itself, which
    proves both that it is serving and that it is still fail-closed.
    """
    expected_host = urlparse(config.web.public_url).netloc
    response = requests.get(
        f"http://{_local_host(config.web.bind)}:{config.web.port}/",
        headers={"Host": expected_host},
        timeout=5,
        allow_redirects=False,
    )
    if config.web.dev_insecure_no_auth:
        if response.status_code != 200:
            raise ValueError(f"Private site listener answered HTTP {response.status_code}")
        return
    if response.status_code != 403 or "default-src 'none'" not in response.headers.get("Content-Security-Policy", ""):
        raise ValueError(f"Private site listener answered HTTP {response.status_code} instead of refusing access")
