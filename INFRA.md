# Newsletter Summary infrastructure

## Production architecture

Newsletter Summary runs on a VM (the Docker VM) on a single standalone Proxmox
host as a Dockge-managed Docker stack. Host names, addresses, and access commands are in `docs/local/homelab.md`
(gitignored; not in the public repo). The persistent app container serves the feeds and the private web UI and is also the
execution target for the shared Ofelia scheduler.

- Dockge stack directory: `/opt/newsletter-summary`
- Live Compose file: `/opt/newsletter-summary/compose.yaml`
- Application copy: `/opt/newsletter-summary/app`
- Runtime config: `/opt/newsletter-summary/config.vm.yaml`
- Credential files: `/opt/newsletter-summary/.env` and
  `/opt/newsletter-summary/secrets/adc.json`
- Persistent state: `/opt/newsletter-summary/var`
- Compose service: `app`
- Container: `newsletter-summary`
- Image: `newsletter-summary:local`
- Shared scheduler stack: `/opt/scheduler`
- Scheduler container: `scheduler`

FreshRSS is a separate stack under `/opt/freshrss`. The Cloudflare Tunnel in
`/opt/edge` routes the public root to FreshRSS and `/digest/*`, `/full/*`,
`/all/*`, `/img/*`, `/newsletters/*`, and `/favicon.ico` to Newsletter Summary
on host port `18791`. The feeds are `/digest/<token>/feed.xml` (daily digest),
`/full/<token>/feed.xml` (reading list), and `/all/<token>/feed.xml`
(everything); `/img/<signature>/<url>` is the signed image proxy. Tokens and
signatures derive from `WEB_SECRET`, which is therefore required. The retired
`/feed.xml` returns 404.
`https://newsletters.merimerimeri.com` goes through Cloudflare Access to the
private web UI on host port `18792`, which the same `serve` process runs on a
second listener (config `web:`, secret `WEB_SECRET` in `.env`). See
[docs/homelab.md](docs/homelab.md#web-ui-and-full-text-feed-november-2026).

## Schedule

Ofelia discovers the job from labels on the persistent app container:

```text
ofelia.enabled=true
ofelia.job-exec.newsletter-summary.schedule=0 0 * * * *
ofelia.job-exec.newsletter-summary.command=uv run --frozen --no-dev newsletter-summary run --config /app/config.yaml
ofelia.job-exec.newsletter-summary.no-overlap=true
```

Ofelia uses six cron fields. The scheduler stack sets
`TZ=America/New_York`; the job runs every hour on the hour. Each run stores
new summaries and email content; the 7:00 PM run closes that day's digest
window (`rss.digest_hour: 19`) and publishes it.
These labels are the only production scheduling source of truth. Do not enable
a parallel systemd timer, cron entry, or macOS LaunchAgent.

Unlike the former systemd job, Ofelia executes the batch in the existing app
container. Data lives in PostgreSQL; the bind-mounted `var` directory holds the
`run` log, the run lock, the image proxy cache, and the retired SQLite archive.

## Deploy

Apply any new `deploy/sql/` migration as the database owner first. Copy the
repository into `/opt/newsletter-summary/app`, copy `deploy/compose.yaml` to
`/opt/newsletter-summary/compose.yaml`, and preserve the machine-local config,
credentials, and state directories. Then run:

```sh
cd /opt/newsletter-summary
docker compose config --quiet
docker compose up -d --build
```

The shared scheduler stack does not need app-specific configuration because it
discovers Docker labels. Ofelia reads labels at startup, so a changed schedule
needs a scheduler reload once no shared job is running.

## Verify

Verify the persistent container, registered labels, scheduler discovery, a real
batch run, and the local/public feed:

```sh
docker ps --filter name=newsletter-summary --filter name=scheduler
docker inspect newsletter-summary --format '{{json .Config.Labels}}'
docker logs --tail 100 scheduler
docker exec newsletter-summary \
  uv run --frozen --no-dev newsletter-summary run --config /app/config.yaml
docker exec newsletter-summary /app/.venv/bin/newsletter-summary health --config /app/config.yaml
```

`health` fetches the digest at its secret URL from the local listener and
checks that the private listener refuses a request without an Access token. To
check the public routes, use the URLs shown on the private site's `/full` page
and keep them out of shell history and logs. The site's `/status` page shows
runs, failures, the last FreshRSS refresh ping, and migration state.

`run` writes its rotating log to `/opt/newsletter-summary/var/newsletter-summary.log`;
the long-running `serve` process logs to `docker logs newsletter-summary` only.
Ofelia's job-start, job-finish, and exit status are written to the shared
`scheduler` container log.

## Recovery

If Ofelia is unavailable, run the exact `docker exec` command above once. Do
not start a second recurring scheduler. Preserve `/opt/newsletter-summary/var`,
`.env`, `config.vm.yaml`, and `secrets/adc.json` when recreating the stack.

## Production database

Production writes to PostgreSQL database `homelab`, schema `newsletter`, on
VM101. `DATABASE_URL_FILE=/run/secrets/database-url` selects PostgreSQL;
`DATABASE_URL` is also supported for development. A configured connection
failure is fatal and never falls back to an old SQLite file. With neither
variable set, the existing SQLite path and local tests still work.

The database is managed by [homelab-backups](https://github.com/stephenmhoc/homelab-backups/tree/main/central-db),
including isolated app credentials, encrypted daily NAS backups and weekly
restore drills. Production's previous SQLite file is a read-only archive.
Use `homelab-db` on the Docker VM to inspect `newsletter.*`. Changes to the
PostgreSQL schema require an explicit central migration; app startup does not
run SQLite DDL against PostgreSQL. Migrations live in `deploy/sql/`; apply each
once, as the database owner, before the deploy that needs it (see the README).
