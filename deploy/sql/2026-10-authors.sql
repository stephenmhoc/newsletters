-- Author names in the daily digest (October 2026). Already applied in
-- production; kept so every PostgreSQL migration lives in deploy/sql/. Run once,
-- as the database owner. Older application versions ignore the column.
ALTER TABLE newsletter.feed_items ADD COLUMN IF NOT EXISTS authors text NOT NULL DEFAULT '';
