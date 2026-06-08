-- Migration: add a region (OVH datacenter) column to pool_hosts.
--
-- Enables region-aware leasing. The lease endpoint can now apply a hard
-- ``region`` equality filter (for direct mngr users who require a specific
-- datacenter) and a soft ``preferred_region`` order-by (for minds, which
-- prefers a closer host but must always fall back to any available host).
-- The value is the OVH datacenter code (e.g. ``US-EAST-VA``) the pool VPS
-- was ordered in, written at bake time by ``mngr imbue_cloud admin pool add``.
--
-- Nullable: rows baked before this migration carry NULL. They never match a
-- hard region filter and sort last under the preferred-region order-by, so
-- they act purely as non-preferred fallback until rebaked.
--
-- No ``IF NOT EXISTS`` guard: the schema_migrations tracking table is the
-- source of truth for which migrations have run (see envs/migrations.py).
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/007_pool_host_region.sql

BEGIN;

ALTER TABLE pool_hosts ADD COLUMN region TEXT;

COMMIT;
