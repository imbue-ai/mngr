-- Migration: add a user-facing host_name column to pool_hosts.
--
-- The new minds shape stops renaming the pre-baked agent at lease time and
-- instead stores the user's chosen workspace name directly on the lease.
-- The connector returns host_name on lease + list so the minds desktop client
-- can populate the mngr ``HostName`` from it without an extra round-trip.
--
-- For pre-existing leased rows, host_name is backfilled with a random
-- ``workspace-<short>`` slug so the column can be NOT NULL without losing
-- prior leases. The slug is intentionally unhelpful -- pre-existing leases
-- date from before the user-facing name concept existed, and assigning real
-- names retroactively isn't possible.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/002_host_name.sql
--
-- Idempotent: rerunning is a no-op once the column exists and is populated.

BEGIN;

ALTER TABLE pool_hosts ADD COLUMN IF NOT EXISTS host_name TEXT;

-- Backfill: every existing row gets a random workspace-<short> slug.
-- ``substr(gen_random_uuid()::text, 1, 8)`` gives 8 hex chars from a
-- fresh UUID, e.g. ``workspace-3f7b9c12``. Skips rows that already have
-- a non-null host_name (so reruns are no-ops).
UPDATE pool_hosts
SET host_name = 'workspace-' || substr(gen_random_uuid()::text, 1, 8)
WHERE host_name IS NULL;

-- Now that every row has a value, the column can be NOT NULL.
ALTER TABLE pool_hosts ALTER COLUMN host_name SET NOT NULL;

-- Per-user uniqueness: a single account can't have two leased hosts with
-- the same name. Pre-existing rows already passed the uniqueness check
-- because the backfill draws from a high-entropy UUID source.
CREATE UNIQUE INDEX IF NOT EXISTS pool_hosts_host_name_per_user_uniq
    ON pool_hosts (leased_to_user, host_name)
    WHERE status = 'leased';

COMMIT;
