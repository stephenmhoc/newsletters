-- Full-text feed and message log (November 2026). Run once, as the database
-- owner, before deploying the version that adds the web UI. The app role
-- cannot run DDL; until this is applied, run/serve/digest keep working and the
-- web UI pages that need these tables show an error.
BEGIN;
CREATE TABLE newsletter.full_items (
  gmail_id text PRIMARY KEY, title text NOT NULL, url text NOT NULL DEFAULT '',
  publication text NOT NULL DEFAULT '', authors text NOT NULL DEFAULT '',
  content_access text NOT NULL DEFAULT 'full', source_gmail_url text NOT NULL DEFAULT '',
  bullets text NOT NULL DEFAULT '', content_html text NOT NULL,
  email_received_at text NOT NULL, added_at text NOT NULL);
CREATE INDEX full_items_added_at ON newsletter.full_items (added_at DESC);
CREATE TABLE newsletter.message_log (
  gmail_id text PRIMARY KEY, subject text NOT NULL DEFAULT '', sender text NOT NULL DEFAULT '',
  gmail_url text NOT NULL DEFAULT '', received_at text NOT NULL DEFAULT '',
  status text NOT NULL, detail text NOT NULL DEFAULT '',
  first_seen_at text NOT NULL, updated_at text NOT NULL);
CREATE INDEX message_log_received_at ON newsletter.message_log (received_at DESC);
GRANT SELECT, INSERT, UPDATE, DELETE ON newsletter.full_items, newsletter.message_log TO newsletter_app;
COMMIT;
