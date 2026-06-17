-- Migration: add slice-backend columns to pool_hosts.
--
-- A pool host is now either a real OVH VPS (backend_kind = 'ovh_vps', the
-- historical default) or a "slice": a lima VM we run on one of our own
-- bare_metal_servers (backend_kind = 'slice'). Leasing is unchanged -- both are
-- ordinary pool_hosts rows matched on attributes. The distinction is consulted
-- only at release: an 'ovh_vps' row cancels its OVH VPS, while a 'slice' row is
-- torn down by SSHing the owning bare-metal box and running `limactl delete` on
-- the named instance + dropping its btrfs disk.
--
-- Existing rows are all real VPSes, so backend_kind defaults to 'ovh_vps'. The
-- bare_metal_server_id / lima_instance_name / lima_disk_name columns are NULL
-- for VPS rows and populated only for slices.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/009_pool_host_slice_columns.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

ALTER TABLE pool_hosts ADD COLUMN backend_kind TEXT NOT NULL DEFAULT 'ovh_vps';
ALTER TABLE pool_hosts ADD COLUMN bare_metal_server_id UUID REFERENCES bare_metal_servers (id);
ALTER TABLE pool_hosts ADD COLUMN lima_instance_name TEXT;
ALTER TABLE pool_hosts ADD COLUMN lima_disk_name TEXT;

-- Slice capacity accounting (slots minus baked slices per server) filters on
-- this pair, so index it.
CREATE INDEX pool_hosts_bare_metal_server_idx ON pool_hosts (bare_metal_server_id);

COMMIT;
