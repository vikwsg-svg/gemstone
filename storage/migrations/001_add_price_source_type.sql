-- PostgreSQL. Adds comp provenance to scraped listings: 'realized' (completed sale) or
-- 'asking' (list price). Required by GemstoneAI Enterprise Comps, which refuses
-- to use any comp that is not tagged.
--
-- The pipeline creates tables with SQLAlchemy create_all(), which does not add
-- columns to existing tables, so existing databases need this script.
-- Run it against a development or staging copy first. Do NOT run it against
-- production without review.
--
-- Backfill policy:
--   * Existing dealer listings default to 'asking' (they are list prices).
--   * Existing auction-house rows are left NULL: the pipeline has not stored
--     prices, and those pages may be pre-sale lots rather than results, so they
--     cannot be assumed 'realized'. NULL rows are excluded from comps.

ALTER TABLE gemstone_items ADD COLUMN price_source_type VARCHAR(16);
CREATE INDEX ix_gemstone_items_price_source_type ON gemstone_items (price_source_type);

UPDATE gemstone_items
SET price_source_type = 'asking'
WHERE price_source_type IS NULL
  AND source_site IN ('leibish.com', 'jamesallen.com', 'bluenile.com');

ALTER TABLE gemstone_items
  ADD CONSTRAINT ck_gemstone_items_price_source_type
  CHECK (price_source_type IS NULL OR price_source_type IN ('realized', 'asking'));
