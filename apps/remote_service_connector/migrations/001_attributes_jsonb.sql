-- Migration: replace pool_hosts.version with a flexible attributes JSONB column.
--
-- The old version-only schema constrained leases on a single text field; the
-- new schema lets the request describe what it wants ({"version":"v1.2.3",
-- "cpus":2, ...}) and the SQL match becomes attributes @> request_attributes.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/001_attributes_jsonb.sql
--
-- Idempotent: rerunning is a no-op once the new column exists.

BEGIN;

ALTER TABLE pool_hosts ADD COLUMN IF NOT EXISTS attributes JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Backfill existing rows: encode the old version string as {"version": "<v>"}
-- so that callers passing {"version": "..."} keep matching their existing rows.
UPDATE pool_hosts
SET attributes = jsonb_build_object('version', version)
WHERE jsonb_typeof(attributes) = 'object'
  AND NOT attributes ? 'version'
  AND version IS NOT NULL;

-- Drop the now-redundant version column. (Skip if it's already gone.)
ALTER TABLE pool_hosts DROP COLUMN IF EXISTS version;

-- Add a GIN index so attributes @> X stays fast as the pool grows.
CREATE INDEX IF NOT EXISTS pool_hosts_attributes_gin ON pool_hosts USING GIN (attributes);

COMMIT;
