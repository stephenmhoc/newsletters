from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from test_cli_run import _config

from newsletter_summary import cli
from newsletter_summary.digest import PUBLICATION_KEY
from newsletter_summary.rss import digest_feed_token
from newsletter_summary.run_state import RunState, check_health, run_alert
from newsletter_summary.store import Store

SECRET = "health-secret"


class RunStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = _config(Path(self.directory.name))
        self.end = datetime.now(UTC)
        self.start = self.end - timedelta(hours=28)
        env = patch.dict("os.environ", {"WEB_SECRET": SECRET})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, result=0, *, target=None, dry_run=False, start=None, end=None):
        with patch("newsletter_summary.cli._message_window", return_value=(start or self.start, end or self.end, "test")), patch(
            "newsletter_summary.cli._run_window", side_effect=result if isinstance(result, Exception) else None,
            return_value=result,
        ) as process:
            code = cli.run(self.config, target, dry_run)
        return code, process.call_args.args[1]

    def test_successful_run_catches_up_after_three_day_outage(self) -> None:
        self._run()
        code, start = self._run(start=self.start + timedelta(days=3), end=self.end + timedelta(days=3))
        self.assertEqual(code, 0)
        self.assertEqual(start, self.end - timedelta(hours=4))
        self.assertEqual(RunState(self.config).data["last_window_end"], (self.end + timedelta(days=3)).isoformat())

    def test_failed_and_interrupted_runs_retain_full_retry_window(self) -> None:
        for result in [1, RuntimeError("Gmail unavailable")]:
            with self.subTest(result=result):
                if isinstance(result, Exception):
                    with self.assertRaises(RuntimeError):
                        self._run(result)
                else:
                    self._run(result)
                state = RunState(self.config)
                self.assertEqual(state.data["status"], "failed")
                self.assertEqual(state.catchup_start(self.start + timedelta(days=3)), self.start)
        # Simulate a killed process which never reaches finish().
        state.start(self.start, self.end)
        self.assertEqual(RunState(self.config).catchup_start(self.start + timedelta(days=3)), self.start)
        self._run(start=self.start + timedelta(days=3), end=self.end + timedelta(days=3))
        self.assertIsNone(RunState(self.config).data["retry_from"])

    def test_dry_run_and_backfill_do_not_advance_checkpoint(self) -> None:
        self._run()
        before = RunState(self.config).data
        self._run(dry_run=True)
        self._run(target="2026-06-28")
        self.assertEqual(RunState(self.config).data, before)

    def test_changed_gmail_label_does_not_reuse_checkpoint(self) -> None:
        self._run()
        other = replace(self.config, gmail=replace(self.config.gmail, label="Other"))
        self.assertEqual(RunState(other).data, {})

    def test_empty_inbox_still_records_a_successful_run(self) -> None:
        with patch("newsletter_summary.cli.build_gmail_service", return_value=Mock()), patch(
            "newsletter_summary.cli.find_label_id", return_value="Label_123"
        ), patch("newsletter_summary.cli.list_messages_for_window", return_value=[]):
            self.assertEqual(cli.run(self.config, None, False), 0)
        self.assertTrue(RunState(self.config).data["last_success"])
        with Store(self.config.storage.sqlite_path) as store:
            self.assertTrue(store.get_setting(PUBLICATION_KEY))

    def test_health_detects_missing_failed_stale_runs_and_bad_feed(self) -> None:
        self.assertEqual(check_health(self.config), 1)
        self.assertFalse(self.config.storage.sqlite_path.exists())
        self._run()
        response = Mock(content=b"<rss><channel/></rss>")
        with patch("newsletter_summary.run_state.requests.get", return_value=response) as get:
            self.assertEqual(check_health(self.config), 0)
            # The secret digest URL, never the retired /feed.xml.
            self.assertEqual(get.call_args.args[0], f"http://127.0.0.1:0/digest/{digest_feed_token(SECRET)}/feed.xml")
            response.content = b"<html/>"
            self.assertEqual(check_health(self.config), 1)
            response.content = b"not XML"
            self.assertEqual(check_health(self.config), 1)
            response.content = b"<rss><channel/></rss>"
            state = RunState(self.config)
            state.data["last_success"] = (self.end - timedelta(hours=31)).isoformat()
            state._save()
            self.assertEqual(check_health(self.config), 1)
            state.data["last_success"] = self.end.isoformat()
            state.data["status"] = "failed"
            # A failure is only unhealthy once runs have kept failing for the grace period.
            state.data["failing_since"] = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
            state._save()
            self.assertEqual(check_health(self.config), 0)
            state.data["failing_since"] = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
            state._save()
            self.assertEqual(check_health(self.config), 1)

    def test_failure_streak_starts_at_the_first_failed_run_and_clears_on_success(self) -> None:
        self._run(1)
        first = RunState(self.config).data["failing_since"]
        self.assertTrue(first)
        self._run(1)
        self.assertEqual(RunState(self.config).data["failing_since"], first)
        self._run()
        self.assertIsNone(RunState(self.config).data["failing_since"])

    def test_health_checks_the_private_listener_is_up_and_closed(self) -> None:
        from newsletter_summary.config import WebConfig

        self._run()
        config = replace(
            self.config,
            web=WebConfig(enabled=True, bind="0.0.0.0", port=18792, public_url="https://newsletters.example.com"),
        )
        feed = Mock(content=b"<rss><channel/></rss>")
        closed = Mock(status_code=403, headers={"Content-Security-Policy": "default-src 'none'; img-src 'self'"})
        with patch("newsletter_summary.run_state.requests.get", side_effect=[feed, closed]) as get:
            self.assertEqual(check_health(config), 0)
        self.assertEqual(get.call_args.args[0], "http://127.0.0.1:18792/")
        self.assertEqual(get.call_args.kwargs["headers"], {"Host": "newsletters.example.com"})
        # Open to anyone, or down: unhealthy.
        open_site = Mock(status_code=200, headers={})
        with patch("newsletter_summary.run_state.requests.get", side_effect=[feed, open_site]):
            self.assertEqual(check_health(config), 1)
        with patch(
            "newsletter_summary.run_state.requests.get", side_effect=[feed, requests.ConnectionError("refused")]
        ):
            self.assertEqual(check_health(config), 1)

    def test_health_handles_connection_failure_and_wildcard_bind(self) -> None:
        self._run()
        config = replace(self.config, server=replace(self.config.server, host="0.0.0.0"))
        with patch("newsletter_summary.run_state.requests.get", side_effect=requests.ConnectionError("refused")) as get:
            self.assertEqual(check_health(config), 1)
        self.assertEqual(get.call_args.args[0], f"http://127.0.0.1:0/digest/{digest_feed_token(SECRET)}/feed.xml")

    def test_health_fails_without_the_feed_secret_and_never_prints_the_token(self) -> None:
        self._run()
        with patch.dict("os.environ", {"WEB_SECRET": ""}), patch("newsletter_summary.run_state.requests.get") as get:
            self.assertEqual(check_health(self.config), 1)
        get.assert_not_called()
        token = digest_feed_token(SECRET)
        error = requests.ConnectionError(f"refused: /digest/{token}/feed.xml")
        with patch("newsletter_summary.run_state.requests.get", side_effect=error), patch("builtins.print") as printed:
            self.assertEqual(check_health(self.config), 1)
        self.assertNotIn(token, printed.call_args.args[0])


class RunAlertTests(unittest.TestCase):
    def test_alert_for_failed_stale_and_missing_runs_only(self) -> None:
        from zoneinfo import ZoneInfo

        eastern = ZoneInfo("America/New_York")
        now = datetime(2026, 11, 22, 20, 0, tzinfo=UTC)
        fresh = (now - timedelta(hours=25)).isoformat()
        self.assertIsNone(run_alert({"status": "ok", "last_success": fresh}, now=now, timezone=eastern))

        failed = run_alert(
            {"status": "failed", "last_success": fresh, "finished_at": "2026-11-22T00:00:05+00:00"},
            now=now,
            timezone=eastern,
        )
        assert failed is not None
        self.assertEqual(failed.title, "Digest runs failing since Sat Nov 21, 7:00 PM EST")
        self.assertEqual(failed.guid, "newsletter-digest-alert:failed:2026-11-22T00:00:05+00:00")

        # Hourly runs: one alert per failure streak, and none inside the grace period.
        streak = {"status": "failed", "last_success": fresh, "failing_since": "2026-11-22T18:00:00+00:00",
                  "finished_at": "2026-11-22T19:00:00+00:00"}
        self.assertIsNone(run_alert(streak, now=datetime(2026, 11, 22, 19, 30, tzinfo=UTC), timezone=eastern))
        later = run_alert(streak, now=datetime(2026, 11, 22, 21, 30, tzinfo=UTC), timezone=eastern)
        latest = run_alert({**streak, "finished_at": "2026-11-22T21:00:00+00:00"},
                           now=datetime(2026, 11, 22, 22, 30, tzinfo=UTC), timezone=eastern)
        assert later is not None and latest is not None
        self.assertEqual(later.guid, latest.guid)

        old = (now - timedelta(hours=27)).isoformat()
        stale = run_alert({"status": "ok", "last_success": old}, now=now, timezone=eastern)
        assert stale is not None
        self.assertTrue(stale.title.startswith("Digest is stale: last successful run at"))
        self.assertEqual(stale.published, datetime.fromisoformat(old) + timedelta(hours=26))

        never = run_alert({}, now=now, timezone=eastern)
        assert never is not None
        self.assertIn("no successful run", never.title)


if __name__ == "__main__":
    unittest.main()
