# Newsletter Summary

Reads Gmail newsletters with a configured label, distills each issue into one
to three short key-point bullets (usually one or two) with Claude Opus 5.5
(`anthropic/claude-opus-5.5`) via OpenRouter, and publishes RSS feeds for FreshRSS and NetNewsWire. Mail is
ingested every hour; each day's digest appears at 7:00 PM Eastern and lists
every newsletter received from 7:00 PM the prior day until 7:00 PM, with its
title, publication, author(s), reading time, links, and bullets that give the
gist without opening the newsletter. Every email's content is stored at
ingest, so a private site can show any of them instantly in a reading view.
Production runs in Docker on a homelab VM on a
single Proxmox host, and stores its data in PostgreSQL.

- Reader: <https://rss.merimerimeri.com/>
- Feeds, all on the public host behind secret tokens:
  - **Newsletters: Daily Digest**, `https://rss.merimerimeri.com/digest/<token>/feed.xml`: one item per day.
  - **Newsletters: Reading List**, `https://rss.merimerimeri.com/full/<token>/feed.xml`: newsletters and web pages you add, in full.
  - **Newsletters: Everything**, `https://rss.merimerimeri.com/all/<token>/feed.xml`: every summarized newsletter, in full.
- Private site (Cloudflare Access): <https://newsletters.merimerimeri.com/>; its
  `/full` page shows the exact feed URLs, with copy buttons.
- [Homelab operations, recovery, and deployment](docs/homelab.md)

The homelab is one standalone Proxmox host; there is no cluster or HA.
Host names, addresses, and access commands are in `docs/local/homelab.md`
(gitignored; not in the public repo). The Mac mini checkout is for
development; Colima is an optional local tool and does not run production.

## What it serves

**Public listener** (port 18791, `https://rss.merimerimeri.com` through
Cloudflare Tunnel; every other path on that host is FreshRSS):

| Path | What |
| --- | --- |
| `/digest/<token>/feed.xml` | Daily Digest; token `HMAC(WEB_SECRET, "digest-feed")` |
| `/full/<token>/feed.xml` | Reading List; token `HMAC(WEB_SECRET, "full-feed")` |
| `/all/<token>/feed.xml` | Everything; token `HMAC(WEB_SECRET, "all-feed")`, newest `rss.all_feed_max_items` (50) |
| `/img/<signature>/<base64url(url)>` | Signed image proxy, see below |
| `/newsletters/<kind>/`, `/newsletters/icons/<kind>.png` | Public home page and icon per feed (`digest`, `full`, `all`) |
| `/favicon.ico` | Icon |

Everything else, including the retired `/feed.xml`, is 404 (a redirect would
leak the token). Feed tokens are compared in constant time and masked in logs,
as are image paths. Feeds are rendered from the database per request, carry
`ETag` and `Last-Modified` (the newest item's date, not the clock), and answer
conditional requests with 304. Without `WEB_SECRET` nothing but the icons is
served.

**Private site** (port 18792, `https://newsletters.merimerimeri.com` behind
Cloudflare Access). The app itself verifies the Access JWT, the Host, and, on
POST, the Origin and an HMAC CSRF token; it stays off unless all of that is
configured.

| Path | What |
| --- | --- |
| `/` | Today so far (the digest that publishes at 7 PM), then past digests |
| `/digest/YYYY-MM-DD` | One day's digest; each feed item links here |
| `/emails` | Every email the service handled; searches subject, sender, title, key points, publication, and authors |
| `/item/<gmail_id>` | Reading view for any processed email |
| `/read/<gmail_id>` | One click from a feed: GET shows a self-submitting form (no side effects), POST adds to the reading list, then opens the reading view |
| `/item/<gmail_id>/remove` | POST: remove from the reading list |
| `/add?url=<url>` | Add a link: GET shows the URL and a self-submitting form (no side effects; text such as "Title https://…" works too), POST fetches the page and adds it, then opens its reading view |
| `/item/web-<hash>[/remove]` | Reading view for a saved web page; POST `/remove` removes it and its stored content |
| `/full` | Add a link (box, bookmarklet, iPhone Shortcut), the reading list, and the three feed URLs |
| `/status` | Last run and last success, the feed alert, failures and given-up emails (14 days), the last FreshRSS refresh ping, image cache size, warnings (failing runs, no success for 2 hours, migrations not applied), and migration state |

Only Gmail IDs already in the store are ever looked up, and web page IDs
(`web-<hash>`) never reach Gmail. List pages and reading
views are sent with `Cache-Control: private, no-cache, no-transform`; every
other response (forms, errors, POST results) with `no-store, no-transform`. `no-transform` also keeps Cloudflare's Email
Obfuscation from rewriting addresses.

## Digest format

Each newsletter is an `<h3>` whose title opens the reading view, then
"Publication · by authors · N min read · partial preview", the bullets, and
links: **Original** (the article, when the model found one in the email) and
**Read in full** (`/read/<id>`, which adds it to the reading list). A digest
also lists that day's skipped and failed emails under "Not summarized", and a
summary created after its day was published appears in the next digest under
"Late from <day>". When the latest runs have failed for three hours, or no
run has succeeded for 26 hours, the digest feed starts with an alert item.

Publication names are kept stable per sender: among names from one sender
address that are variants of each other ("The Backstory" and "The Backstory
(Puck and Air Mail)"), the most common wins, ties going to the most recent.
Distinct newsletters from one address are left alone. `rss.publication_names`
(sender address to name) overrides this.

## Stored email content

`run` stores every fetched email that has a body (raw HTML and text, sanitized
HTML, sanitizer version, and word count) in `email_content`, before summarizing it. The reading view, the
Reading List, and the Everything feed use it, with no Gmail call. Emails from
before it existed are fetched from Gmail when first opened, then stored.

When `SANITIZER_VERSION` (in `full_content.py`) is bumped, stored content is
re-derived from the raw email: lazily whenever it is read, or all at once:

```bash
uv run newsletter-summary resanitize --config config.yaml        # stale rows only
uv run newsletter-summary resanitize --config config.yaml --all  # every row
```

`full_items` holds reading list membership and metadata; its `content_html`
is only a fallback for rows added before the content store existed (or while
its migration is not applied). nh3 is the security boundary and runs last at
render time.

## Add a link

Any public web page can go on the reading list: paste its URL in the box on
`/full`, use the bookmarklet shown there
(`javascript:location.href='https://newsletters.merimerimeri.com/add?url='+encodeURIComponent(location.href)`),
or on iPhone a Shortcut on the share sheet that runs **URL Encode** on the
Shortcut Input and then **Open URLs** with
`https://newsletters.merimerimeri.com/add?url=<URL-encoded input>`.

POST `/add` (Origin and CSRF checked) fetches the page through the same
guarded fetcher as the image proxy (`safe_fetch.py`: http/https on ports
80/443, every resolved address and the connected peer public, at most three
re-checked redirects, 20 seconds), accepting only HTML or XHTML up to 5 MB
after gzip/deflate decompression, decoded with its declared or sniffed charset. readability-lxml extracts the
article; the title, byline, site name (`og:site_name`, else the host),
published date, lead image (`og:image`), and canonical URL (`rel=canonical`,
else the final URL, without subscriber tokens) come from the page's meta tags
and JSON-LD. The
article goes through the newsletter sanitizer (nh3 last) and the image proxy,
and is stored in `email_content` like an email, so `resanitize` re-derives it.
Under 150 words it is still saved, marked partial, with a note to open the
original. The item ID is `web-<first 20 hex of sha256(canonical URL)>`, so a
page is saved once however it was reached. Key points come from the same
model and bullet rules, in the background, followed by the FreshRSS refresh
ping; a model failure leaves the item without them. Fetch failures (blocked address, not HTML, too large, timeout,
HTTP error) show an error page and store nothing. Web items appear in the
Reading List feed (GUID `newsletter-web:<hash>`, linked to the canonical URL),
never in the digest or the Everything feed.

## Image proxy

Images in the reading view and in the Reading List and Everything feeds load
through `/img/<signature>/<base64url(url)>` on the public host (the signature
is a truncated `HMAC(WEB_SECRET, "image-proxy" NUL url)`), so
senders see neither opens nor the reader's IP address, and cached images
survive link rot. Only URLs the app signed are fetched, over http/https on
ports 80/443. Every resolved address must be public, and the connected peer is
checked again before any request is sent (DNS rebinding). Redirects are capped
at three and each is re-checked. Responses must be at most `images.max_image_mb` (10 MB) after
decompression, arrive within 20 seconds, and be JPEG, PNG, GIF, WebP, or AVIF by their bytes (never
SVG). Images are cached under `images.cache_dir` (LRU, bounded by
`images.cache_max_mb`) and served with a one-year immutable `Cache-Control`.
The private site's CSP allows images only from itself, `data:` URIs, and the
proxy origin. Failed fetches are not retried for 10 minutes.
Feed icons are not proxied. Set `images.proxy: false` to load images directly.

## Production checks

Run these **inside the Docker VM**, or wrap them in `qm guest exec <vmid> -- ...`
on the Proxmox host:

```bash
docker ps --filter name=newsletter-summary --filter name=freshrss --filter name=scheduler
docker exec newsletter-summary /app/.venv/bin/newsletter-summary health --config /app/config.yaml
docker exec newsletter-summary uv run --frozen --no-dev newsletter-summary doctor --config /app/config.yaml
docker logs --since 6h scheduler 2>&1 | grep 'Job "newsletter-summary"'
```

`health` fetches the digest from the local listener at its secret URL (do not
put that URL in shell history) and checks that the private listener answers
403 without an Access token, which shows it is up and still fail-closed. The
private site's `/status` page shows the same state in more detail.

Ofelia runs the summarizer **every hour on the hour** (America/New_York). The
7:00 PM run closes and publishes the day's digest. FreshRSS checks
subscriptions at minutes **3 and 33** of every hour, subject to each
subscription's refresh interval. A healthy producer does not prove FreshRSS is
reading it: check both containers' health and each subscription's last update.

Subscribe FreshRSS to the exact HTTPS feed URLs shown on the private site's
`/full` page. Change an existing subscription's URL in place to preserve its
read/star state. Removing an item from the reading list does not remove it
from FreshRSS, which keeps articles it already fetched.

## Production database

Production writes to PostgreSQL database `homelab`, schema `newsletter`, on
the Docker VM. `DATABASE_URL_FILE=/run/secrets/database-url` selects PostgreSQL;
`DATABASE_URL` is also supported for development. A configured connection
failure is fatal and never falls back to SQLite. With neither variable set, the
SQLite path in `storage.sqlite_path` is used (local development and tests).

The database is managed by [homelab-backups](https://github.com/stephenmhoc/homelab-backups/tree/main/central-db),
including isolated app credentials, encrypted daily NAS backups and weekly
restore drills. Production's previous SQLite file is a read-only archive.
Use `homelab-db` on the Docker VM to inspect `newsletter.*`.

The app role cannot run DDL. PostgreSQL migrations live in
[`deploy/sql/`](deploy/sql/); each is additive and is run once, as the database
owner, before the deploy that needs it. SQLite applies the same changes
automatically (`Store._migrate`). Until a migration is applied, the app keeps
working with the dependent feature off (only "relation or column does not
exist" counts as not migrated), and `/status` lists it as missing:

| File | Adds | Without it |
| --- | --- | --- |
| `2026-10-authors.sql` | `feed_items.authors` (already applied) | |
| `2026-11-full-text.sql` | `full_items`, `message_log` | no reading list, no email log or "Not summarized" list |
| `2026-11-23-message-attempts.sql` | `message_log.attempts` | failing summaries are retried every run instead of three times |
| `2026-11-24-email-content.sql` | `email_content` | no stored content: the reading view fetches from Gmail on every open, the reading list keeps its own copy, the Everything feed has no full text, no reading time |
| `2026-11-25-web-links.sql` | `full_items.source_kind`, `full_items.source_url` | "Add a link" says it is not installed and stores nothing |

Bullets are stored one per line in `feed_items.highlights`; rows from before
bullets still render their paragraph.

## FreshRSS refresh pings

FreshRSS polls the Reading List feed on its own schedule. When
`FRESHRSS_REFRESH_URL` (the env var named by `web.reader_refresh_url_env`)
holds FreshRSS's token-authenticated `actualize` URL for that feed, the site
requests it after every add or removal (for a web page, once its key points
are stored), then again 70 seconds later, since FreshRSS reuses its cached copy
of a feed for at least 60 seconds. The URL is never logged; `/status` shows the
last ping's result.

## Local development

```bash
uv sync --frozen --group dev
cp config.example.yaml config.yaml
```

Edit the local config and provide `OPENROUTER_API_KEY` through the environment
or ignored `.env`. On macOS, the configured Keychain fallback is also supported.
Never commit credentials, machine config, or generated runtime data.

Gmail uses Application Default Credentials with the read-only scope:

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/cloud-platform
uv run newsletter-summary doctor --config config.yaml
uv run newsletter-summary run --config config.yaml --dry-run
uv run newsletter-summary run --config config.yaml
uv run newsletter-summary serve --config config.yaml
```

The first run searches the latest 28 hours. Later runs catch up from their last
successful processing window, retaining a four-hour overlap. A failed or
interrupted run retains its full window for retry. Gmail IDs prevent duplicate
summaries and repeat OpenRouter charges: emails already summarized, or given
up on, are not fetched again.

To backfill one day's digest (7:00 PM the prior day until 7:00 PM on the date):

```bash
uv run newsletter-summary run --config config.yaml --date 2026-06-28
```

Dry runs and date-specific backfills do not advance the normal run checkpoint.
A message whose summary fails is listed under "Not summarized" and retried by
the next runs, up to three attempts in all (`MAX_SUMMARY_ATTEMPTS`, counted in
`message_log.attempts`; three hours with hourly runs). After that it is given
up on: it is no longer fetched, no longer holds the window open or fails the
run, and `/status` lists it as given up. `resummarize` cannot retry it, since it
only replaces existing summaries. Only one
`run` executes at a time; a second one exits with an error.

To re-summarize existing emails with the current prompt (for example, to give
paragraph-era rows bullets):

```bash
uv run newsletter-summary resummarize --config config.yaml --since 2026-11-15 --dry-run
uv run newsletter-summary resummarize --config config.yaml --since 2026-11-15 [--legacy-only] [--yes]
```

It prints the number of emails, how many must first be fetched from Gmail, and
an estimate of model calls and tokens, then asks for confirmation (`--yes`
skips the prompt; without a terminal it is required). Each email's summary is
replaced in one transaction and keeps its original publication time, so it
stays in the digest it was published in. A failure keeps the old summary. It
takes the run lock.

Once a normal run has succeeded and the feed server is running:

```bash
uv run newsletter-summary health --config config.yaml
```

`health` requires a successful normal run within `--max-age-hours` (default
30; production uses 3), no run failures lasting three hours, `WEB_SECRET`,
valid RSS from the local digest feed URL, and, when the site is enabled, a
fail-closed private listener. It works on days with no new mail and does not
call Gmail or OpenRouter. `doctor` checks Gmail access, the OpenRouter key,
and `WEB_SECRET`, and exits nonzero when any is missing or failing.

Optional Mac-only setup remains available through `scripts/start-freshrss.sh`
and `deploy/freshrss.compose.yml`. These are not homelab deployment instructions.

## Development checks

```bash
python3 -m compileall src
uv run python -m unittest discover -s tests
uv run ruff check .
uv run ty check
```

The lint, type, and test checks also run in GitHub Actions. Use `uv sync --frozen
--group dev` to install the declared tools.

## Processing and storage

- Gmail is read-only: messages are never archived, relabeled, or marked read.
- The exact configured label is used; the default is `Newsletters/Skipped`.
- Each email produces one issue-level summary: article title, publication,
  author(s), a link that appeared in the email, one to three key-point bullets,
  and a partial/paywall label. The prompt (`summary.prompt` plus the rules in
  `openrouter._bullet_rules`) asks for one or two bullets in most cases, three
  only for three separate ideas, each one sentence of about 20 words and never
  more than 30; anything past three bullets is dropped. A link the model returns is accepted only if it
  matches one from the email after both are normalized; otherwise (logged at
  INFO, without its query string) the summary links to the reading view when
  the site is enabled, else to the message in Gmail (`#all/`).
- Links are normalized without re-encoding: known redirectors (including
  Substack's `/redirect/2/` links) are unwrapped, and subscriber tokens and
  tracking parameters are dropped. Opaque click trackers are left as they are.
- The model gets the email as Markdown without link URLs (the links are listed
  separately), cut at `summary.max_input_chars` (default 200,000) with a note
  saying it was cut.
- The digest feed has one item per published day, newest first, with GUID
  `newsletter-digest:YYYY-MM-DD`. A day is published when the run that closes
  its window finishes, so readers never see a half-built item; hourly runs
  before then only store summaries. Days with nothing to show produce no item.
  `rss.digest_hour` sets the window boundary and `rss.max_items` caps the
  number of days.
- The Everything feed has the newest `rss.all_feed_max_items` summarized
  emails, one item each, GUID `newsletter-all:<gmail_id>`, dated when the email
  arrived; it updates every hour, before the digest is published.
- Fetch and summary errors are counted, processing continues for healthy
  messages, and the command exits nonzero. Empty model output is also a failure.
- Summaries, stored content, run checkpoints, the per-day publication record,
  and the last FreshRSS ping live in the database. `run` appends to
  `storage.log_path`, which rotates at 5 MB with three backups; `serve` logs
  only to stdout (`docker logs`), so only one process ever rotates the file.
- Local SQLite uses rollback journaling, so a read-only backup mount does not
  need to create WAL shared-memory sidecars.
- FreshRSS owns subscriptions and read/star state separately from the producer.
