-- Migration: add the bare_metal_servers table.
--
-- Tracks the OVH bare-metal (dedicated) servers we rent and carve into lima-VM
-- "slices". Each slice is an ordinary pool_hosts row (see migration 009) that
-- references its owning bare-metal server here. The admin tooling
-- (`mngr imbue_cloud admin server ...`) writes these rows directly, laptop-side,
-- mirroring how pool_hosts rows are written today; the connector only reads them
-- (and, at slice release, uses the joined-in lima fields on pool_hosts).
--
-- Resumable lifecycle: `status` advances ordered -> delivered -> installing ->
-- ready (or failed) and is moved forward by re-running the admin command, so a
-- slow OVH order delivery or a long OS install can be picked up across separate
-- invocations.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/008_bare_metal_servers.sql
--
-- No IF NOT EXISTS guard: schema_migrations (see apps/minds/.../envs/migrations.py)
-- is the source of truth for which migrations have run.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE bare_metal_servers (
    id UUID PRIMARY KEY,
    -- OVH order id captured at checkout (e.g. "8144904"); present from the
    -- "ordered" state onward so a slow delivery can be polled/reconciled.
    ovh_order_id TEXT,
    -- OVH dedicated-server serviceName, assigned during delivery; NULL until
    -- the order is delivered. Every connector/admin OVH call keys on this.
    ovh_service_name TEXT,
    -- The catalog planCode the box was ordered as (e.g. "24rise02-v1-us").
    plan_code TEXT NOT NULL,
    -- OVH datacenter code (e.g. "vin"); used for placement and OVH region calls.
    region TEXT NOT NULL,
    -- SSH-reachable public address of the box (IPv4 or DNS); NULL until known.
    public_address TEXT,
    -- Detected hardware, used to derive slot_count and per-slice sizing.
    cpu_cores INTEGER,
    cpu_threads INTEGER,
    ram_gb INTEGER,
    -- floor(ram_gb / 8): the number of 8GB slices this box can hold.
    slot_count INTEGER NOT NULL DEFAULT 0,
    -- RAID level configured at OS-install time (e.g. "RAID1", "RAID10").
    raid_level TEXT,
    -- The dedicated non-root OS user that owns the lima VMs on this box; both
    -- the admin CLI and the connector SSH in as this user to drive limactl.
    lima_service_user TEXT,
    -- Resumable lifecycle state: ordered | delivered | installing | ready | failed.
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX bare_metal_servers_status_idx ON bare_metal_servers (status);
CREATE UNIQUE INDEX bare_metal_servers_service_name_idx
    ON bare_metal_servers (ovh_service_name)
    WHERE ovh_service_name IS NOT NULL;

COMMIT;
