-- Migration: rename pool_hosts.vps_ip to pool_hosts.vps_address.
--
-- The column holds an SSH-reachable address that can be a public IPv4
-- (Vultr-backed rows) or a DNS hostname (OVH-backed rows return the
-- OVH serviceName, e.g. ``vps-eec8860b.vps.ovh.us``). The old name
-- was actively misleading once OVH became the default pool backend,
-- so this migration renames the column and the matching index (if
-- any) to match the new connector / mngr_imbue_cloud field name.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/003_vps_address.sql
--
-- Idempotent: rerunning is a no-op once the column has been renamed.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'pool_hosts' AND column_name = 'vps_ip'
    ) THEN
        ALTER TABLE pool_hosts RENAME COLUMN vps_ip TO vps_address;
    END IF;
END
$$;

COMMIT;
