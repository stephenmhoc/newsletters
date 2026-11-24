# Homelab operations

**Storage:** since September 27, production uses PostgreSQL database `homelab`,
schema `newsletter`, via `DATABASE_URL_FILE`, for summaries, stored email
content, run state, and the reading list. SQLite is only for local development
and the retained read-only pre-migration archive; vendor FreshRSS keeps its own
SQLite database. Central backup
and recovery instructions are in [homelab-backups](https://github.com/stephenmhoc/homelab-backups/tree/main/central-db).
Do not roll storage back without reconciling writes made after migration.

## Placement and routes

The homelab is a **single standalone Proxmox host**; there is no Corosync
cluster, quorum, HA, or replication, and the Docker VM does not move.
Host names, addresses, and access commands are in `docs/local/homelab.md`
(gitignored; not in the public repo). See
homelab-backups `RECOVERY.md` for host-loss recovery from PBS.

Production is a VM (the Docker VM) on a homelab VLAN, also reachable over
Tailscale. Operate it through the guest agent on the Proxmox host:

```bash
ssh root@<proxmox-host> /usr/sbin/qm guest exec <vmid> -- /usr/bin/docker ps --format '{{.Names}} {{.Status}}'
ssh root@<proxmox-host> /usr/sbin/qm guest cmd <vmid> network-get-interfaces
```

Use full paths: a non-root or non-login SSH session may not have `/usr/sbin` on
`PATH`. `qm guest exec` accepts up to 1 MiB on stdin with `--pass-stdin 1`, which is
enough to upload a `git archive` of this repository.

| Component | Guest location / container | Role |
| --- | --- | --- |
| Producer | `/opt/newsletter-summary`, `newsletter-summary` | Gmail summaries, RSS server (18791), private web UI (18792) |
| Scheduler | shared `scheduler` container | Ofelia discovers the producer's Docker labels |
| Reader | `/opt/freshrss`, `freshrss` | Subscriptions, entries, read/star state |
| Public edge | `/opt/edge`, `edge-cloudflared` | Cloudflare Tunnel using host networking |

Cloudflare routes these paths on `https://rss.merimerimeri.com/` to guest
loopback `18791`: `/digest/*` (daily digest), `/full/*` (reading list),
`/all/*` (everything), `/img/*` (image proxy), `/newsletters/*` (feed icons and
home pages), and `/favicon.ico`. All other paths on that hostname go to guest
loopback `18790` (FreshRSS). The app binds `0.0.0.0:18791` inside its
container; Docker only publishes that port on the guest's loopback interface.

FreshRSS subscribes to the digest at
`https://rss.merimerimeri.com/digest/<token>/feed.xml`, where the token is
`HMAC(WEB_SECRET, "digest-feed")`, and likewise to `/full/` and `/all/`; the
private site's `/full` page shows the exact URLs. The old `/feed.xml` returns 404 (no redirect, which would leak the
token). Each refresh also checks the public route, so it depends on the tunnel
and public DNS. `host.docker.internal` was a Mac/Colima address and is not a
working route in this deployment.

### Web UI, reading list, and Everything feed (November 2026)

The producer also listens on guest loopback **18792** (`web.port`) for the
private UI. Cloudflare Tunnel routes `https://newsletters.merimerimeri.com/` to
it, behind a Cloudflare Access application; the app independently verifies the
`Cf-Access-Jwt-Assertion` JWT (RS256 keys from
`https://<web.access_team_domain>/cdn-cgi/access/certs`, `aud` = `web.access_aud`,
email in `web.allowed_emails`) and the Host. Inside the container set
`web.bind: "0.0.0.0"`, as for the feed port. Routes: `/` today so far and
digests, `/digest/YYYY-MM-DD` one day, `/emails` every processed email,
`/item/<gmail_id>` the reading view for any of them, `/read/<gmail_id>` (GET
shows an auto-submitting form; POST adds to the reading list),
`/item/<gmail_id>/remove`, `/full` the reading list and the three feed URLs,
`/add` ("Add a link": GET `/add?url=…` shows an auto-submitting form; POST
fetches the page and adds it), and `/status` (runs, failures, FreshRSS pings,
migrations). Web pages get IDs `web-<hash>` and never reach Gmail. Adding one
makes an outbound HTTPS request from the container to that site (public
addresses only, HTML only, at most 5 MB in 20 seconds) and one OpenRouter call
for its key points; no new egress rule or Cloudflare change is needed.

The reading list and Everything feeds are served by the public listener at
`https://rss.merimerimeri.com/full/<token>/feed.xml` and
`https://rss.merimerimeri.com/all/<token>/feed.xml`. Subscribe FreshRSS to the
exact URLs shown on `/full`; the tokens are `HMAC(WEB_SECRET, "full-feed")` and
`HMAC(WEB_SECRET, "all-feed")`. Rotating `WEB_SECRET` changes every feed URL
and every proxied image URL. `WEB_SECRET` comes from the stack's private
`.env`; without it no feed is served and the UI is off.

Images in the reading view and in those two feeds load through the signed
proxy at `/img/...` on the public host, cached in
`/opt/newsletter-summary/var/image-cache` (bounded by `images.cache_max_mb`).
Deleting that directory is safe; images are fetched again on demand.

FreshRSS subscribes to the reading list feed as feed 22, with a 15-minute TTL. So that an add
shows up right away, `FRESHRSS_REFRESH_URL` in the producer's `.env` holds
`https://rss.merimerimeri.com/i/?c=feed&a=actualize&id=22&ajax=1&user=stephen&token=<token>`,
where the token is FreshRSS's user token (`cli/update-user.php --token`). The site
requests it after every add or removal (for a web page, once its key points are
stored), then again 70 seconds later; `/status` shows the last result. FreshRSS
reuses its cached copy of a feed for `limits.cache_duration` seconds, which is
lowered to its 60-second minimum in `/opt/freshrss` `data/config.php` so these
refreshes see new items. Never log or print the URL.

Before deploying, apply any new PostgreSQL migration in `deploy/sql/` once, as
the database owner (the README lists them and what each enables; "Add a link"
needs `2026-11-25-web-links.sql`). The app keeps
working without them, with the dependent feature off.

The retired Mac LaunchAgents and Tailscale Funnel setup, and the local Colima
FreshRSS compose file, are not production controls. Do not run another
scheduled summarizer there.

## Schedule and health

Ofelia uses the schedule `0 0 * * * *` (second, minute, hour, day, month,
weekday: every hour on the hour), with `TZ=America/New_York` on the shared
scheduler. It runs:

```bash
uv run --frozen --no-dev newsletter-summary run --config /app/config.yaml
```

The producer label `ofelia.job-exec.newsletter-summary.no-overlap=true`
prevents overlapping scheduled runs. A manual `docker exec ... run` is outside
Ofelia's lock, but `run` takes its own PostgreSQL advisory lock: a manual run
during the scheduled one exits with "Another run is in progress".
Each hourly run stores new summaries and email content; the 7:00 PM run (the
producer's `rss.digest_hour: 19`) closes the day's window and publishes that
digest when it finishes. A run at any other time only publishes digests for
windows that have already closed, so the open day appears only under "Today
so far" on the private site and in the Everything feed. A summary stored after its day was published appears in the
next digest under "Late from <day>"; skipped and failed emails are listed under
"Not summarized". A summary that fails three runs is given up on (attempts are
counted in `message_log.attempts`), so it cannot hold the window open. With
hourly runs that is three hours. Nothing retries it afterwards: `resummarize`
only replaces existing summaries.
Ofelia 0.3.22 reads labels at startup and executes jobs by container name. A
rebuild retaining the `newsletter-summary` name, command, and schedule works
without a scheduler restart. Changed labels or a new job require reloading the
scheduler after checking that no shared jobs are running. See the release's
[label loader](https://github.com/mcuadros/ofelia/blob/v0.3.22/cli/docker-labels.go),
[daemon](https://github.com/mcuadros/ofelia/blob/v0.3.22/cli/daemon.go), and
[exec implementation](https://github.com/mcuadros/ofelia/blob/v0.3.22/core/execjob.go).

FreshRSS's built-in cron runs at minutes 3 and 33. Feed-specific TTL settings
can defer individual refreshes.

Both containers have Docker health checks every five minutes, with two failures
required before Docker marks them unhealthy:

- Producer: the local digest feed at its secret URL (so `WEB_SECRET` must be
  set) must be valid RSS, runs must not have been failing for three hours, a
  successful normal run must be less than 3 hours old (`--max-age-hours 3` in
  the compose health check), and the private listener on 18792 must answer
  403 to a request without an Access token (up and fail-closed). The digest
  feed itself shows one alert item per failure streak of three hours, or after
  26 hours without a successful run.
  An empty inbox is still a successful run. Failed/interrupted runs retain
  their processing window; successful windows overlap by four hours.
- FreshRSS: its built-in HTTP check must pass, exactly one subscription whose
  URL starts with `https://rss.merimerimeri.com/digest/` must exist without a
  fetch error, and its last successful refresh must be less than two hours old.
  The prefix match keeps the token out of the compose file.

These checks expose failures through Docker/Dockge. Docker health status alone
does not restart a container or send a notification.

Inside the Docker VM:

```bash
docker inspect newsletter-summary freshrss --format '{{.Name}} {{json .State.Health}}'
docker exec newsletter-summary /app/.venv/bin/newsletter-summary health --config /app/config.yaml
docker exec newsletter-summary uv run --frozen --no-dev newsletter-summary doctor --config /app/config.yaml
docker logs --since 6h scheduler 2>&1 | grep 'Job "newsletter-summary"'
tail -80 /opt/newsletter-summary/var/newsletter-summary.log   # `run` log
docker logs --since 1h newsletter-summary                       # `serve` log
```

The producer's `doctor` verifies Gmail access, OpenRouter key presence, and `WEB_SECRET`; a
successful real run proves the OpenRouter call works. Neither an HTTP 200 nor
a green producer container alone proves FreshRSS is ingesting summaries. The
private site's `/status` page shows the last run and success, recent failures
and given-up emails, the last FreshRSS refresh ping, and missing migrations.

Run the FreshRSS subscription check directly:

```bash
docker exec --user www-data freshrss php /usr/local/bin/freshrss-newsletter-health.php \
  --database /var/www/FreshRSS/data/users/stephen/db.sqlite \
  --feed-url-prefix https://rss.merimerimeri.com/digest/
```

## Recovery

1. Check scheduler logs, producer health, and FreshRSS subscription health to
   identify which stage failed.
2. For producer auth failures, repair the credential mount/environment without
   printing the credentials. Gmail uses read-only ADC at `/secrets/adc.json`;
   OpenRouter uses `OPENROUTER_API_KEY` from the stack's private `.env`.
3. Run the producer manually when no scheduled run is active. It automatically
   catches up from its saved window. An upgrade from a version without a
   checkpoint starts with 28 hours; older gaps require explicit `--date` runs.
4. If the producer is current but the reader is stale, edit the existing
   Newsletters: Daily Digest subscription (formerly Newsletter Highlights) to the digest URL shown on the private site's `/full` page.
   Preserve the subscription instead of deleting/re-adding it.
5. Refresh the feed in FreshRSS and verify its error clears, last update moves
   forward, and entries match the producer. Existing GUIDs prevent duplicates.

Manual producer commands inside the guest:

```bash
docker exec newsletter-summary uv run --frozen --no-dev newsletter-summary run --config /app/config.yaml --dry-run
docker exec newsletter-summary uv run --frozen --no-dev newsletter-summary run --config /app/config.yaml
# Example digest backfill (7 PM on 2026-08-22 to 7 PM on 2026-08-23); choose the actual missing date.
docker exec newsletter-summary uv run --frozen --no-dev newsletter-summary run --config /app/config.yaml --date 2026-08-23
```

FreshRSS's supported user refresh command (it respects feed TTLs and can
refresh other due subscriptions):

```bash
docker exec --user www-data freshrss php cli/actualize-user.php --user stephen
```

## Deployment and rollback

The checked-in `deploy/compose.yaml` is the homelab producer template. Install
it at `/opt/newsletter-summary/compose.yaml` and keep source under
`/opt/newsletter-summary/app`. It builds `deploy/Dockerfile` and uses the frozen
lockfile. `.dockerignore` excludes runtime files and credentials.

Before deploying, inspect the deployed source and compose file for local
changes. Back up the current source and compose (the central PostgreSQL
database has its own backups); retain the previous image under a rollback tag.
Apply any new `deploy/sql/` migration as the database owner first. Never replace `/opt/newsletter-summary/var`,
`.env`, `secrets/`, or `config.vm.yaml` with checkout contents.

Run repository tests, Ruff, and ty locally. Copy the reviewed source and producer
compose template into the locations above, then inside the guest:

```bash
cd /opt/newsletter-summary
docker compose config --quiet
docker compose build app
docker compose up -d --no-deps app
```

Run the producer once after first installing checkpoint support, then verify
`health`, public RSS, and FreshRSS ingestion. Confirm the new producer container
has the Ofelia labels and the next scheduled execution succeeds. An unchanged
job does not produce a new registration log on a worker-only rebuild.

For FreshRSS, copy `deploy/freshrss-newsletter-health.php` to
`/opt/freshrss/freshrss-newsletter-health.php`. Merge the volume and healthcheck
from `deploy/freshrss.healthcheck.yaml` into `/opt/freshrss/compose.yaml`, preserving
its existing environment, data mounts, and ports. Keeping the merged settings
in the main file ensures Dockge retains them when recreating the stack.
Validate the compose and PHP syntax before `docker compose up -d freshrss`.

A rollback restores the previous source/compose and recreates the affected
container from the retained image. The checkpoint lives in the existing
`settings` table and requires no schema migration. Restore a database only when
needed; a wholesale restore would discard entries/read-state changes made
since the backup.

## State and backups

Producer data lives in PostgreSQL (`homelab.newsletter`): summaries, every
processed email's raw and sanitized content (`email_content`), the email log,
and the reading list. Run status, the catch-up checkpoint, the per-day digest
publication record, and the last FreshRSS ping are in its `settings` table;
feeds are rendered from the database on each request, so no feed file is
written. `/opt/newsletter-summary/var/` holds the `run` log,
`newsletter-summary.log` plus rotated backups, the run lock, the image proxy
cache, and the retired SQLite archive.

FreshRSS state lives under `/opt/freshrss/data`; the current user database is
`users/stephen/db.sqlite`. These databases contain private content. Use SQLite's
backup API for consistent snapshots while services are live. The producer uses
rollback journaling to support the central backup container's read-only mounts.

The related local homelab runbooks are
`/Users/agent/Development/homelab/homelab-backups/INFRA.md` and
`/Users/agent/Development/homelab/homelab-monitoring/README.md`. Backup-container
health is separate from newsletter ingestion; inspect it before assuming a
recent off-host backup exists.

## Verification record: 2026-09-05

The producer and Ofelia were running daily, including September 4 at 10:00 AM
Eastern (four new summaries, zero failures). Gmail auth succeeded and the public
feed was current. The producer and FreshRSS databases passed `PRAGMA quick_check`.

FreshRSS's Newsletter Highlights subscription still pointed to
`http://host.docker.internal:18791/feed.xml` and had not successfully refreshed
since August 15 at 18:03 Eastern. That hostname failed to resolve inside the
Linux FreshRSS container; the canonical HTTPS URL returned valid RSS there.

After a consistent SQLite backup, the subscription was updated in place and
refreshed. It imported **80 missing summaries**, increasing cached entries from
172 to 252 and clearing its fetch error. All previous entries and read/star
states were preserved. No daily summarizer outage caused this reader backlog.

The repaired deployment's manual run completed at 09:53 Eastern with four new
summaries and zero failures; FreshRSS imported all four, bringing its newsletter
cache to 256 entries. Both Docker health checks passed. A Gmail audit from
August 15 through the verification time found 86 currently labeled messages
and no messages missing from the producer's store.

The normal **10:00 AM scheduled run** then completed automatically in 2.7
seconds with `failed: false`. Its saved checkpoint advanced to 10:00 AM and
both containers remained healthy. The shared scheduler did not need restarting.

The Python suite passed 43 tests, Ruff, type checking, and compilation. The
FreshRSS PHP check passed syntax validation and live/fixture cases for healthy,
stale, failed, and missing subscriptions. Rollback source, compose, database,
and image snapshots are recorded in
`/opt/newsletter-summary/revisions/current-deployment.json`.

The separate `homelab-sqlite-backup` container reported unhealthy during the
initial inventory. Its off-host backup status was not investigated in this
newsletter repair; the consistent local repair snapshots succeeded.
