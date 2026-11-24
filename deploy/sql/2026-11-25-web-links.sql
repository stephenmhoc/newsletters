-- Web links on the reading list (November 2026). Run once, as the database
-- owner, before deploying the version with "Add a link" (/add). Additive only.
-- Until it is applied, everything else keeps working: adding a web page shows
-- a "not installed yet" page and stores nothing, and /status lists it as
-- missing. Requires deploy/sql/2026-11-full-text.sql (full_items); web page
-- content goes in email_content (deploy/sql/2026-11-24-email-content.sql)
-- when that is installed.
BEGIN;
ALTER TABLE newsletter.full_items ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'gmail';
ALTER TABLE newsletter.full_items ADD COLUMN IF NOT EXISTS source_url text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS full_items_source_url ON newsletter.full_items (source_url);
COMMIT;
