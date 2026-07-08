-- Migration: add per-slice sizing inputs to bare_metal_servers.
--
-- A box's slice sizing is no longer hardcoded. Each server records the RAM per
-- slice it advertises (``memory_per_slice_gb``, which sets ``slot_count`` and the
-- per-slice memory), a CPU overcommit factor (``cpu_overcommit_ratio``), and its
-- usable disk (``disk_gb``); the per-slice vCPU / memory / disk are computed from
-- these plus the detected specs. These are admin-only inputs (the connector does
-- not read them); they are NULL for any server registered before this migration
-- (re-register or update such a row before allocating slices on it).
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/010_bare_metal_slice_sizing.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

ALTER TABLE bare_metal_servers ADD COLUMN disk_gb INTEGER;
ALTER TABLE bare_metal_servers ADD COLUMN memory_per_slice_gb INTEGER;
ALTER TABLE bare_metal_servers ADD COLUMN cpu_overcommit_ratio DOUBLE PRECISION;

COMMIT;
