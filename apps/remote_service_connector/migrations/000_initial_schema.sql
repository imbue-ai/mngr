-- Migration 000: initial pool_hosts schema.
--
-- Idempotent canonical schema for a fresh database. Designed to roll
-- forward cleanly when followed by migrations 001/002/003 (each of
-- which is a defensive ALTER that no-ops when the column / state is
-- already what 000 produced). So the bootstrap order for any new
-- database is: 000 -> 001 -> 002 -> 003, regardless of whether 000 or
-- a pre-000 manual CREATE seeded the table.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/000_initial_schema.sql
--
-- ``minds env deploy`` runs this automatically for dev-env Neon
-- projects via ``apply_pool_hosts_schema`` in the neon provider.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS pool_hosts (
    id UUID PRIMARY KEY,
    vps_address TEXT NOT NULL,
    vps_instance_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    host_name TEXT NOT NULL,
    ssh_port INTEGER NOT NULL,
    ssh_user TEXT NOT NULL,
    container_ssh_port INTEGER NOT NULL,
    status TEXT NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    leased_to_user TEXT,
    leased_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS pool_hosts_attributes_gin ON pool_hosts USING GIN (attributes);
CREATE INDEX IF NOT EXISTS pool_hosts_host_name_idx ON pool_hosts (host_name);

COMMIT;
