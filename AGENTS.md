# AGENTS.md

## Project Overview

This Python app reads Gmail newsletters with a configured label, distills each
issue into one to three short key-point bullets (usually one or two, about 20
words each, never more than 30) with Claude Opus 5.5 via OpenRouter,
stores them in PostgreSQL in production (SQLite locally), and serves three RSS
feeds for FreshRSS and NetNewsWire: **Newsletters: Daily Digest** (one entry
per day, published at 7:00 PM Eastern, listing every newsletter received since
7:00 PM the prior day with title, publication, authors, reading time, links,
and bullets), **Newsletters: Reading List** (newsletters and web pages added on the site, in
full), and **Newsletters: Everything** (every summarized newsletter as its own
item, in full). The bullets are meant to replace reading the issue.
`summary.model` must be an OpenRouter `anthropic/claude-*` model. There are no
read/skip recommendations. Production runs in Docker on a homelab VM (the Docker VM).
The Mac mini checkout is for development, not the production runtime.

Read [docs/homelab.md](docs/homelab.md) for the verified topology, deployment,
health checks, and recovery. The homelab is a single standalone Proxmox host
(no cluster, quorum, or HA); the Docker VM is reached with `qm guest exec` on
that host. Host names, addresses, and access commands are in `docs/local/homelab.md`
(gitignored; not in the public repo).

## Production Layout

Inside the Docker VM:

- `/opt/newsletter-summary/compose.yaml`: Dockge app stack; template in `deploy/compose.yaml`.
- `/opt/newsletter-summary/app/`: Docker build context, including `deploy/Dockerfile`.
- `/opt/newsletter-summary/config.vm.yaml`: private machine config, mounted at `/app/config.yaml`.
- `/opt/newsletter-summary/.env` and `secrets/adc.json`: private credentials.
- `/opt/newsletter-summary/var/`: the `run` log, the run lock file, and the
  image proxy cache (`var/image-cache`). Data and checkpoints live in PostgreSQL.
- `/opt/freshrss/`: separate FreshRSS stack and persistent user data.
- `/opt/edge/`: separate Cloudflare Tunnel stack.
- `scheduler`: shared Ofelia container, with `TZ=America/New_York`.

The app container `newsletter-summary` runs the feed server continuously.
Ofelia executes `run` inside it every hour on the hour (`0 0 * * * *`,
`no-overlap=true`). Each run summarizes new mail and stores every processed
email's content; the run that closes a day's window (`rss.digest_hour: 19`,
so the 7:00 PM run) publishes that day's digest when it finishes. `run`
records that moment per day (settings key `digest_publication`), and every
feed is rendered from the database on each request. `run` also holds a run
lock (a PostgreSQL advisory lock, or a file lock beside the SQLite database),
so a manual run or `resummarize` during the scheduled one exits instead of
summarizing twice. Do not add a second cron scheduler or restart the shared
scheduler without a demonstrated need.

The guest publishes `127.0.0.1:18791` (public listener), `127.0.0.1:18792`
(private site), and `127.0.0.1:18790` (FreshRSS). At
`https://rss.merimerimeri.com/`, Cloudflare routes `/digest/*`, `/full/*`,
`/all/*`, `/img/*`, `/newsletters/*`, and `/favicon.ico` to 18791 and
everything else to 18790. The Cloudflare container uses host networking. The
app binds `0.0.0.0` inside its container.

The public listener serves only the feeds at `/digest/<token>/feed.xml`,
`/full/<token>/feed.xml`, and `/all/<token>/feed.xml`, the signed image proxy
at `/img/<signature>/<base64url(url)>`, the feed icons and home pages under
`/newsletters/`, and `/favicon.ico`. Feed tokens are
`HMAC(WEB_SECRET, "<kind>-feed")` for `digest`, `full`, and `all`, compared in
constant time and masked in logs; never log or commit them. Image paths are
masked too: the URLs inside can carry subscriber tokens. `/feed.xml` is retired
and returns 404 (a redirect would leak the token). Without `WEB_SECRET` no feed
is served. Feeds send `ETag`/`Last-Modified` and answer 304. The digest adds an
alert item when runs have failed for three hours or none has succeeded for 26
hours. Each digest lists skipped and failed emails under "Not summarized", and
a summary created after its day was published goes into the next digest under
"Late from <day>".

The image proxy (`image_proxy.py`, guards in `safe_fetch.py`) fetches only URLs the app signed, and only
over http/https on ports 80/443 to public addresses: it resolves the host and
checks every address, then checks the connected peer again before sending
anything (DNS rebinding), re-checks each redirect (at most three), caps size
(`images.max_image_mb`) and time, and accepts only JPEG/PNG/GIF/WebP/AVIF by
their bytes, never SVG. Keep those guards and their tests.

`serve` also runs the private site on a second listener, guest loopback
`18792` (`web:` config, `WEB_SECRET` env), published at
`https://newsletters.merimerimeri.com` behind Cloudflare Access. The app
verifies the Access JWT, Host, Origin, and CSRF tokens itself; keep it
fail-closed. Its routes are `/` (today so far, then digests),
`/digest/YYYY-MM-DD`, `/emails`, `/full` (reading list and feed URLs),
`/status`, `/read/<gmail_id>` (GET has no side effects; POST adds),
`/item/<id>[/remove]`, and `/add` ("Add a link": GET `/add?url=` has no side
effects and auto-submits; POST, with Origin and CSRF, fetches and saves any
web page). Web pages are fetched only through `safe_fetch.py` (the same guards
as the image proxy; HTML only, 5 MB), extracted with readability-lxml
(`web_article.py`), sanitized like email, stored in `email_content` under the
ID `web-<sha256(canonical URL)[:20]>`, and summarized in the background with
`openrouter.summarize_article`. Web IDs never reach Gmail: keep `_known` and
every Gmail fetch Gmail-ID-only. Web items are in the Reading List feed only,
never the digest or Everything feed. `/item` is the reading view for any processed
email. Only Gmail IDs already in the store may be fetched from Gmail. Content
comes from `email_content` (written by `run`; re-derived from the stored raw
email when `SANITIZER_VERSION` in `full_content.py` is bumped, lazily or with
`resanitize`). `full_items` is reading list membership and metadata; its
`content_html` is only a fallback for older rows. Sanitized HTML comes from
`full_content.py`: nh3 is the security boundary and runs last at render time,
with proxy rewriting done inside nh3's attribute filter. Feed and site HTML for
digest entries comes from one renderer, `render.py`.

PostgreSQL migrations live in `deploy/sql/`; the database owner applies each
once before the deploy that needs it (the app role cannot run DDL). The app
keeps working without them, with the dependent feature off: only "relation or
column does not exist" errors count as "not migrated". Mirror every migration
in `Store._migrate` and bump `SCHEMA_VERSION`. `/status` shows which are
installed.

A summary that fails is retried by later runs up to `MAX_SUMMARY_ATTEMPTS`
(3) times, counted in `message_log.attempts`; then `run` gives up on it and
`/status` lists it. `resummarize` only replaces existing summaries, so it does
not retry those.

When `FRESHRSS_REFRESH_URL` (`web.reader_refresh_url_env`) is set, the site
pings that FreshRSS `actualize` URL after each reading list add or removal
(for a web page, after its key points are stored) and again 70 seconds later.
The URL carries a FreshRSS token: never log or print it.

FreshRSS must subscribe to the feed URLs shown on the site's `/full` page.
`host.docker.internal` was the obsolete Mac address and failed in the Linux
container. Edit subscriptions in place to preserve read/star state.

## Commands

Use `uv` locally:

```bash
uv sync --frozen --group dev
uv run newsletter-summary doctor --config config.yaml
uv run newsletter-summary run --config config.yaml --dry-run
uv run newsletter-summary run --config config.yaml
uv run newsletter-summary serve --config config.yaml
uv run newsletter-summary health --config config.yaml
uv run newsletter-summary resanitize --config config.yaml [--all]
uv run newsletter-summary resummarize --config config.yaml --since YYYY-MM-DD --dry-run
```

`resummarize` calls OpenRouter once per email: always run `--dry-run` first and
get the owner's go-ahead before running it for real.

Prompt changes (`summary.prompt`, `openrouter._bullet_rules`) are measured with
the eval in `evals/newsletter_bullets/` (cases, grading, and results in
`CASES.md`). A run calls OpenRouter, about $1.27 for 8 cases x 2 reps.

The first normal run searches 28 hours. Subsequent normal runs catch up from a
durable checkpoint, retaining failed/interrupted windows. `--date D` backfills
the digest window ending at the digest hour on D. Dry runs and `--date`
backfills do not advance the checkpoint. Health is based on successful execution, not the
age of the newest email, so quiet inbox days remain healthy.

Validation:

```bash
python3 -m compileall src
uv run python -m unittest discover -s tests
uv run ruff check .
uv run ty check
```

Lint, type, test, and gitleaks secret-scan checks also run in
`.github/workflows/checks.yml`. Run `gitleaks git --redact .` before pushing;
keep host names, addresses, and access details in gitignored `docs/local/`.

## Credentials and State

Never print or commit keys, passwords, `.env`, `config.yaml`, `config.vm.yaml`,
`secrets/`, `var/`, generated feeds, logs, or SQLite state. `.venv/` is ignored.
Use the shareable `config.example.yaml` and `.env.example` templates.

Production supplies `OPENROUTER_API_KEY` through the stack environment and
`GOOGLE_APPLICATION_CREDENTIALS=/secrets/adc.json` through a read-only mount.
Gmail credentials require the Gmail read-only scope. The macOS Keychain and
Colima are supported local-development options, not production services.
`WEB_SECRET` is required in production: it derives the feed URLs and signs image proxy URLs and CSRF tokens.

Production uses `homelab.newsletter` through `DATABASE_URL_FILE`; see the central
[database runbook](https://github.com/stephenmhoc/homelab-backups/tree/main/central-db).
Its old SQLite file is read-only. Run state and health must use PostgreSQL too.

For local SQLite, back up with the SQLite backup API before repairs; do not copy only
the main file while a writer may be active. Preserve production's rollback
journal mode, which supports the central backup container's read-only mounts.

## Git and Deployment Hygiene

Before committing:

```bash
git status --short --branch
git diff --cached --stat
```

Keep commits scoped. Inspect deployed source/config changes before replacing
files, validate Compose without printing resolved secrets, and retain rollback
copies. Verify producer output, scheduler execution, and FreshRSS ingestion
separately. An HTTP 200 or running container alone does not prove freshness.
