-- Summary attempt counts (November 2026). Run once, as the database owner,
-- before deploying the version that gives up on an email after three failed
-- summaries. Additive only. Until it is applied, run/serve/digest keep
-- working; failed summaries are retried every run, as before, and `run` logs a
-- warning. Requires deploy/sql/2026-11-full-text.sql (message_log).
ALTER TABLE newsletter.message_log ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
