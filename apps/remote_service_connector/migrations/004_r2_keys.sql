-- Migration 004: r2_keys table for R2 bucket-scoped key metadata.
--
-- Tracks the *existence* of each bucket-scoped S3 key so the connector can
-- list and revoke them. Stores only the non-secret Access Key ID (= the
-- Cloudflare token id) plus metadata -- NEVER the secret / token value.
--
-- Buckets themselves are NOT tracked here; they are listed straight from the
-- R2 API (name_contains filter + an in-code owner-prefix re-check).
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/004_r2_keys.sql
--
-- Idempotent: rerunning is a no-op once the table + indexes exist.

BEGIN;

CREATE TABLE IF NOT EXISTS r2_keys (
    access_key_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    bucket_name TEXT NOT NULL,
    access TEXT NOT NULL CHECK (access IN ('read', 'readwrite')),
    alias TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS r2_keys_owner_idx ON r2_keys (owner_user_id);
CREATE INDEX IF NOT EXISTS r2_keys_owner_bucket_idx ON r2_keys (owner_user_id, bucket_name);

COMMIT;
