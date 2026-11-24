-- Stored email content (November 2026). Run once, as the database owner,
-- before deploying the version with the reading view for every email, the
-- "Newsletters: Everything" feed, and resummarize/resanitize. Additive only.
-- Until it is applied, run/serve/digest keep working: `run` logs a warning and
-- stores no content, the reading view fetches from Gmail on every open, the
-- reading list keeps storing its own copy in full_items.content_html, and the
-- Everything feed carries key points without the full text.
-- Requires deploy/sql/2026-11-full-text.sql (full_items).
BEGIN;
CREATE TABLE IF NOT EXISTS newsletter.email_content (
  gmail_id text PRIMARY KEY,
  subject text NOT NULL DEFAULT '',
  sender text NOT NULL DEFAULT '',
  received_at text NOT NULL DEFAULT '',
  raw_html text NOT NULL DEFAULT '',
  raw_text text NOT NULL DEFAULT '',
  content_html text NOT NULL DEFAULT '',
  sanitizer_version integer NOT NULL DEFAULT 0,
  word_count integer NOT NULL DEFAULT 0,
  stored_at text NOT NULL);
GRANT SELECT, INSERT, UPDATE, DELETE ON newsletter.email_content TO newsletter_app;
-- Reading list rows now keep membership and metadata only; their body comes
-- from email_content. Older rows keep their copy as the fallback.
ALTER TABLE newsletter.full_items ALTER COLUMN content_html SET DEFAULT '';
COMMIT;
