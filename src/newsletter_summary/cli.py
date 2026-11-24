from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from datetime import date, datetime, time, timedelta

from .config import AppConfig, load_config
from .digest import digest_window, record_publication
from .email_content import ContentWriter, current_content, resanitize_all, save_message
from .email_extract import email_to_markdown, extract_links, extract_web_version_links
from .full_content import SANITIZER_VERSION
from .gmail_client import (
    GMAIL_ID_PATTERN,
    GMAIL_READONLY_SCOPE,
    NewsletterMessage,
    build_gmail_service,
    fetch_message,
    find_label_id,
    gmail_link,
    list_messages_for_window,
)
from .openrouter import SummaryItem, summarize_message
from .platform_server import serve_platform
from .rss import email_link, web_secret
from .run_state import RunState, check_health
from .secrets import get_secret
from .store import MAX_SUMMARY_ATTEMPTS, RunLockBusy, Store, StoredContent, StoreFeatureUnavailable, run_lock

__all__ = ["MAX_SUMMARY_ATTEMPTS"]

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
# Rough prompt size around each email body (instructions, links), and output
# per summary, for resummarize's estimate. About four characters per token.
PROMPT_OVERHEAD_TOKENS = 2_500
OUTPUT_TOKENS_PER_SUMMARY = 400
CHARS_PER_TOKEN = 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="newsletter-summary")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config.yaml", help="Path to YAML config.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", parents=[common], help="Fetch recent newsletters, summarize them, and publish closed digests."
    )
    run_parser.add_argument(
        "--date",
        help=(
            "Digest date to backfill, YYYY-MM-DD: mail from the digest hour on the prior day "
            "until the digest hour on this date. Defaults to catching up from the last run."
        ),
    )
    run_parser.add_argument(
        "--dry-run", action="store_true", help="Fetch and parse, but do not call OpenRouter or publish."
    )

    subparsers.add_parser("doctor", parents=[common], help="Check config, auth, label, and local serving state.")
    health_parser = subparsers.add_parser("health", parents=[common], help="Check the feed server and scheduled-run freshness.")
    health_parser.add_argument("--max-age-hours", type=float, default=30)

    serve_parser = subparsers.add_parser("serve", parents=[common], help="Serve the feeds and the private web UI.")
    serve_parser.add_argument("--host", help="Override configured host.")
    serve_parser.add_argument("--port", type=int, help="Override configured port.")

    resanitize_parser = subparsers.add_parser(
        "resanitize",
        parents=[common],
        help="Re-derive stored email content with the current sanitizer (stale rows only, unless --all).",
    )
    resanitize_parser.add_argument("--all", action="store_true", help="Re-sanitize every stored email.")

    resummarize_parser = subparsers.add_parser(
        "resummarize",
        parents=[common],
        help="Re-summarize stored emails with the current prompt, replacing their summaries.",
    )
    resummarize_parser.add_argument("--since", required=True, help="First local date to include, YYYY-MM-DD.")
    resummarize_parser.add_argument(
        "--dry-run", action="store_true", help="Only print how many emails would be re-summarized and the estimate."
    )
    resummarize_parser.add_argument(
        "--legacy-only", action="store_true", help="Only emails whose summary is a paragraph from before bullets."
    )
    resummarize_parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")

    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"Error: could not load config {args.config!r}: {exc}", file=sys.stderr)
        return 1
    if args.command != "health":
        # The long-lived server logs to stdout only (docker logs rotates it);
        # only short-lived commands append to the rotating file, so two
        # processes never rotate the same file.
        setup_logging(config, to_file=args.command != "serve")

    try:
        if args.command == "run":
            return run(config, target=args.date, dry_run=args.dry_run)
        if args.command == "doctor":
            return doctor(config)
        if args.command == "health":
            return check_health(config, args.max_age_hours)
        if args.command == "serve":
            return serve(config, host=args.host, port=args.port)
        if args.command == "resanitize":
            return resanitize(config, force=args.all)
        if args.command == "resummarize":
            return resummarize(
                config, since=args.since, dry_run=args.dry_run, legacy_only=args.legacy_only, assume_yes=args.yes
            )
    except Exception as exc:
        logging.exception("Command failed")
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def setup_logging(config: AppConfig, *, to_file: bool = True) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if to_file:
        config.storage.log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.insert(
            0,
            logging.handlers.RotatingFileHandler(
                config.storage.log_path,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            ),
        )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def run(config: AppConfig, target: str | None, dry_run: bool) -> int:
    if dry_run:
        return _run_unlocked(config, target, dry_run)
    try:
        # Overlapping runs (a manual run during the scheduled one) would
        # summarize the same emails twice and race on the checkpoint.
        with run_lock(config.storage.sqlite_path):
            return _run_unlocked(config, target, dry_run)
    except RunLockBusy:
        logging.warning("Another run is in progress; not starting a second one")
        print("Another run is in progress; try again when it finishes.", file=sys.stderr)
        return 1


def _run_unlocked(config: AppConfig, target: str | None, dry_run: bool) -> int:
    start, end, window_description = _message_window(config, target)
    state = RunState(config)
    if not target:
        start = state.catchup_start(start)
        window_description = f"{start.isoformat()} to {end.isoformat()}"
    # Only a real rolling-window run advances the schedule's checkpoint. A
    # dry run or historical backfill cannot make an overdue job look healthy.
    tracked = not target and not dry_run
    if tracked:
        state.start(start, end)
    try:
        result = _run_window(config, start, end, window_description, dry_run)
    except Exception:
        if tracked:
            state.finish(success=False)
        raise
    if tracked:
        state.finish(success=result == 0)
    return result


def _run_window(config: AppConfig, start: datetime, end: datetime, window_description: str, dry_run: bool) -> int:
    logging.info("Processing label %r for %s", config.gmail.label, window_description)

    service = build_gmail_service()
    label_id = find_label_id(service, config.gmail.user_id, config.gmail.label)
    message_ids = list_messages_for_window(service, config.gmail.user_id, label_id, start, end)
    logging.info("Gmail returned %s candidate messages", len(message_ids))

    # Check the store before fetching: emails already summarized, or given up
    # on, are not downloaded again.
    with Store(config.storage.sqlite_path) as store:
        known_ids = store.stored_gmail_ids()
        gave_up = _gave_up_ids(store)
    already = [message_id for message_id in message_ids if message_id in known_ids]
    abandoned = [message_id for message_id in message_ids if message_id in gave_up and message_id not in known_ids]
    for message_id in abandoned:
        logging.info(
            "Not retrying %s: its summary failed %s times; it is listed as not summarized",
            message_id,
            MAX_SUMMARY_ATTEMPTS,
        )
    pending = [message_id for message_id in message_ids if message_id not in known_ids and message_id not in gave_up]

    messages = []
    failed = 0
    # (gmail_id, status, detail) for messages that could not be fetched, and
    # messages without a body; recorded in message_log, never during a dry run.
    unreadable: list[tuple[str, str, str]] = []
    no_body: list[NewsletterMessage] = []
    for message_id in pending:
        try:
            message = fetch_message(service, config.gmail.user_id, message_id, config.gmail.tzinfo)
        except Exception as exc:
            # One unreadable message must not abort the whole batch.
            logging.exception("Could not fetch Gmail message %s; skipping it", message_id)
            failed += 1
            unreadable.append((message_id, "fetch_failed", f"{type(exc).__name__}: {exc}"))
            continue
        if message is None or not message.has_body:
            if message is None:
                unreadable.append((message_id, "skipped_no_body", "No HTML or text body"))
            else:
                no_body.append(message)
        elif start <= message.received_at < end:
            messages.append(message)
    logging.info(
        "%s new messages are inside the processing window; %s were already summarized",
        len(messages),
        len(already),
    )

    if dry_run:
        for message in messages:
            links = extract_links(message.html, message.text)
            print(f"{message.received_at.isoformat()} | new | {message.subject} | {len(links)} links")
        print(f"Dry run found {len(messages)} new message(s); {len(already)} duplicate(s); {failed} failed.")
        return 1 if failed else 0

    store = Store(config.storage.sqlite_path)
    log = _MessageLog(store)
    contents = ContentWriter(store)
    added = 0
    skipped = len(already) + len(abandoned)
    try:
        for message_id, status, detail in unreadable:
            log.record_id(message_id, status, detail)
        for message in no_body:
            log.record(message, "skipped_no_body", "No HTML or text body")
        for message in messages:
            # Stored first, so even an email whose summary fails can be read on the site.
            contents.save(message)
            links = extract_links(message.html, message.text)
            if not links:
                logging.info("Skipping %s: no usable links", message.subject)
                skipped += 1
                log.record(message, "skipped_no_links", "No usable links in the email")
                continue
            try:
                summary = _summarize(config, message, links)
            except Exception as exc:
                # Keep going so one bad summary does not cost the whole day's
                # digest; the message is retried by later runs, up to
                # MAX_SUMMARY_ATTEMPTS times.
                logging.exception("Failed to summarize %s; skipping it", message.subject)
                failed += 1
                log.record(message, "summary_failed", f"{type(exc).__name__}: {exc}", count_attempt=True)
                continue
            if store.add_summary(message, summary):
                added += 1
            else:
                skipped += 1
            log.record(message, "summarized")
        # Publish every digest whose window has now closed. Feeds and pages
        # are rendered from the database on each request.
        record_publication(store, config, now=datetime.now(config.gmail.tzinfo))
    finally:
        store.close()

    logging.info(
        "Stored %s new summaries and %s emails' content; %s skipped; %s failed", added, contents.saved, skipped, failed
    )
    print(f"Stored {added} new summary(ies); {skipped} skipped; {failed} failed.")
    return 1 if failed else 0


def _summarize(config: AppConfig, message: NewsletterMessage, links=None) -> SummaryItem:
    """One OpenRouter call; raises when the summarizer returns nothing usable."""
    web_public_url = config.web.public_url if config.web.enabled else ""
    links = extract_links(message.html, message.text) if links is None else links
    summary = summarize_message(
        api_key_env=config.summary.openrouter_api_key_env,
        keychain_service=config.summary.openrouter_keychain_service,
        keychain_account=config.summary.openrouter_keychain_account,
        model=config.summary.model,
        max_input_chars=config.summary.max_input_chars,
        app_name=config.summary.app_name,
        site_url=config.summary.site_url,
        base_prompt=config.summary.prompt,
        message=message,
        body_markdown=email_to_markdown(message.html, message.text),
        links=links,
        web_links=extract_web_version_links(message.html),
        fallback_url=email_link(web_public_url, message.gmail_id),
    )
    if summary is None:
        raise ValueError("The summarizer returned no newsletter item")
    return summary


def _gave_up_ids(store: Store) -> set[str]:
    try:
        return store.gave_up_ids(MAX_SUMMARY_ATTEMPTS)
    except StoreFeatureUnavailable:
        # Without message_log.attempts every failure is retried, as before.
        return set()


class _MessageLog:
    """Best-effort message_log writes: a logging failure never fails the run."""

    def __init__(self, store: Store):
        self.store = store
        self.available = True

    def record(self, message: NewsletterMessage, status: str, detail: str = "", *, count_attempt: bool = False) -> None:
        self._write(
            message.gmail_id,
            status=status,
            detail=detail,
            subject=message.subject,
            sender=message.sender,
            gmail_url=message.gmail_url,
            received_at=message.received_at.isoformat(),
            count_attempt=count_attempt,
        )

    def record_id(self, gmail_id: str, status: str, detail: str = "") -> None:
        self._write(gmail_id, status=status, detail=detail)

    def _write(self, gmail_id: str, *, count_attempt: bool = False, **fields: str) -> None:
        if not self.available:
            return
        try:
            self.store.log_message(gmail_id, count_attempt=count_attempt, **fields)
        except Exception as exc:
            # For example, PostgreSQL before deploy/sql/2026-11-full-text.sql.
            logging.warning("Could not write message_log (%s); continuing without it this run", exc)
            self.available = False


def doctor(config: AppConfig) -> int:
    print(f"Config: {config.path}")
    print(f"Gmail label: {config.gmail.label}")
    print(f"Timezone: {config.gmail.timezone}")
    print(f"Feed server: http://{config.server.host}:{config.server.port}/ (digest at /digest/<token>/feed.xml)")
    print(f"Public feed host: {config.rss.public_url}")
    print(f"Feed secret (${config.web.secret_env}): {'set' if web_secret(config) else 'MISSING: feeds are not served'}")
    openrouter_key = get_secret(
        config.summary.openrouter_api_key_env,
        config.summary.openrouter_keychain_service,
        config.summary.openrouter_keychain_account,
    )
    source = "available" if openrouter_key else "missing"
    healthy = bool(openrouter_key) and bool(web_secret(config))
    print(f"OpenRouter key: {source}")
    if config.summary.openrouter_keychain_service:
        print(f"OpenRouter Keychain service: {config.summary.openrouter_keychain_service}")

    try:
        service = build_gmail_service()
        label_id = find_label_id(service, config.gmail.user_id, config.gmail.label)
        print("Gmail auth: ok")
        print(f"Gmail label id: {label_id}")
    except Exception as exc:
        healthy = False
        print("Gmail auth/label: failed")
        print(f"  {exc}")
        print(
            "  Suggested auth refresh:\n"
            f"  gcloud auth application-default login --scopes={GMAIL_READONLY_SCOPE},https://www.googleapis.com/auth/cloud-platform"
        )
    return 0 if healthy else 1


def resanitize(config: AppConfig, *, force: bool = False) -> int:
    with Store(config.storage.sqlite_path) as store:
        try:
            done, skipped = resanitize_all(store, force=force)
        except StoreFeatureUnavailable as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    print(f"Re-sanitized {done} stored email(s) with sanitizer version {SANITIZER_VERSION}; {skipped} had no raw body.")
    return 0


def message_from_content(content: StoredContent) -> NewsletterMessage:
    """A stored email as the summarizer takes it, without a Gmail call."""
    return NewsletterMessage(
        gmail_id=content.gmail_id,
        thread_id="",
        subject=content.subject,
        sender=content.sender,
        received_at=datetime.fromisoformat(content.received_at),
        html=content.raw_html,
        text=content.raw_text,
        gmail_url=gmail_link(content.gmail_id),
    )


def resummarize(
    config: AppConfig, *, since: str, dry_run: bool, legacy_only: bool = False, assume_yes: bool = False
) -> int:
    """Re-summarize emails received on or after `since` with the current prompt.

    Each email's summary rows are replaced in one transaction, keeping the
    original publication time, so it stays in the digest it was published
    in. Bodies come from the content store; emails stored before it existed
    are fetched from Gmail (and stored). The count and a token estimate are
    printed first; nothing is sent without --yes or a confirmation.
    """
    try:
        first_day = date.fromisoformat(since)
    except ValueError:
        print("Error: --since must be a date, YYYY-MM-DD", file=sys.stderr)
        return 1
    start = datetime.combine(first_day, time(0), tzinfo=config.gmail.tzinfo)
    with Store(config.storage.sqlite_path) as store:
        candidates = [
            summary
            for summary in store.list_summaries()
            if summary.email_received_at >= start
            and GMAIL_ID_PATTERN.match(summary.source_gmail_id)
            and (not legacy_only or not summary.bullets)
        ]
        stored = {summary.source_gmail_id: current_content(store, summary.source_gmail_id) for summary in candidates}
    legacy = sum(1 for summary in candidates if not summary.bullets)
    missing = [gmail_id for gmail_id, content in stored.items() if content is None]
    body_tokens = sum(
        len(email_to_markdown(content.raw_html, content.raw_text)) // CHARS_PER_TOKEN
        for content in stored.values()
        if content is not None
    )
    known = len(candidates) - len(missing)
    # Emails without stored content are estimated at the average of the rest.
    if missing:
        body_tokens += (body_tokens // known if known else 5_000) * len(missing)
    input_tokens = body_tokens + PROMPT_OVERHEAD_TOKENS * len(candidates)
    print(
        f"{len(candidates)} email(s) received since {first_day.isoformat()} would be re-summarized with "
        f"{config.summary.model} ({legacy} paragraph-era); {len(missing)} must be fetched from Gmail first."
    )
    print(
        f"Estimate: {len(candidates)} model call(s), about {input_tokens:,} input and "
        f"{OUTPUT_TOKENS_PER_SUMMARY * len(candidates):,} output tokens."
    )
    if dry_run or not candidates:
        return 0
    if not assume_yes:
        if not sys.stdin.isatty():
            print("Not confirmed: pass --yes to run without a prompt.", file=sys.stderr)
            return 1
        if input("Re-summarize them now? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("Cancelled.")
            return 1
    try:
        with run_lock(config.storage.sqlite_path):
            return _resummarize_locked(config, [summary.source_gmail_id for summary in candidates])
    except RunLockBusy:
        print("A run is in progress; try again when it finishes.", file=sys.stderr)
        return 1


def _resummarize_locked(config: AppConfig, gmail_ids: list[str]) -> int:
    replaced = failed = 0
    service = None
    with Store(config.storage.sqlite_path) as store:
        log = _MessageLog(store)
        for gmail_id in gmail_ids:
            try:
                content = current_content(store, gmail_id)
                if content is not None:
                    message = message_from_content(content)
                else:
                    service = service or build_gmail_service()
                    message = fetch_message(service, config.gmail.user_id, gmail_id, config.gmail.tzinfo)
                    if not message.has_body:
                        raise ValueError("The email has no readable body")
                    try:
                        save_message(store, message)
                    except StoreFeatureUnavailable:
                        pass
                summary = _summarize(config, message)
            except Exception as exc:
                # The old summary stays; nothing is deleted before a new one exists.
                logging.exception("Could not re-summarize %s; keeping its old summary", gmail_id)
                print(f"Failed {gmail_id}: {type(exc).__name__}", file=sys.stderr)
                failed += 1
                continue
            if store.replace_summary(message, summary):
                replaced += 1
                log.record(message, "summarized")
    print(f"Re-summarized {replaced} email(s); {failed} failed.")
    return 1 if failed else 0


def serve(config: AppConfig, host: str | None, port: int | None) -> int:
    serve_platform(config, host, port)
    return 0


def _target_day(config: AppConfig, target: str | None) -> date:
    if target:
        return date.fromisoformat(target)
    return datetime.now(config.gmail.tzinfo).date()


def _message_window(config: AppConfig, target: str | None) -> tuple[datetime, datetime, str]:
    if target:
        target_day = _target_day(config, target)
        start, end = digest_window(target_day, config.gmail.tzinfo, config.rss.digest_hour)
        return start, end, f"the {target_day.isoformat()} digest ({start.isoformat()} to {end.isoformat()})"
    end = datetime.now(config.gmail.tzinfo)
    start = end - timedelta(hours=28)
    return start, end, f"{start.isoformat()} to {end.isoformat()}"


if __name__ == "__main__":
    raise SystemExit(main())
