-- Migration: add a region (OVH datacenter) column to pool_hosts.
--
-- Enables region-aware leasing. The lease endpoint can apply a hard
-- ``region`` equality filter (for callers who require a specific datacenter);
-- when no region is requested the lease is region-agnostic. The value is the
-- OVH datacenter code (e.g. ``US-EAST-VA``) the pool VPS was ordered in,
-- written at bake time by ``mngr imbue_cloud admin pool add``.
--
-- Nullable: rows baked before this migration carry NULL. They never match a
-- hard region filter, so they are only leased when no region is requested,
-- until rebaked.
--
-- No ``IF NOT EXISTS`` guard: the schema_migrations tracking table is the
-- source of truth for which migrations have run (see envs/migrations.py).
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/007_pool_host_region.sql

BEGIN;

ALTER TABLE pool_hosts ADD COLUMN region TEXT;

COMMIT;
