-- Migration: add a mutable, user-facing host_name to pool_hosts.
--
-- Minds now drives the workspace identity off the host name (the literal
-- string the user types in the create-project form) rather than the agent
-- name. The connector needs to round-trip that name through the lease so
-- downstream surfaces (mngr list, future rename, etc.) see what the user
-- chose, not the opaque host_id UUID.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/002_host_name.sql
--
-- Idempotent: rerunning is a no-op once the column exists.

BEGIN;

-- Add the column as nullable first so the backfill can run without violating
-- the NOT NULL constraint on existing rows.
ALTER TABLE pool_hosts ADD COLUMN IF NOT EXISTS host_name TEXT;

-- Backfill existing rows to the opaque host_id so they remain visible in
-- mngr list under the same label they currently use (the lease-fallback
-- path in the imbue_cloud provider previously synthesized `HostName(host_id)`
-- as the display name).
UPDATE pool_hosts
SET host_name = host_id
WHERE host_name IS NULL;

ALTER TABLE pool_hosts ALTER COLUMN host_name SET NOT NULL;

-- Index for the lookup path that resolves a friendly name to a leased row.
CREATE INDEX IF NOT EXISTS pool_hosts_host_name_idx ON pool_hosts (host_name);

COMMIT;
