-- Migration 013: workspace-sync tables (workspace records + account key bundles).
--
-- workspace_records: one row per (user, host) workspace, holding plaintext
-- metadata (name, color, provider, location, lifecycle state) plus an opaque
-- client-side-encrypted secrets blob. The server can never read the secrets;
-- it stores and serves them verbatim. Writes are compare-and-swap on the
-- per-row revision counter. At most one ACTIVE row may exist per
-- (user_id, agent_id) -- a restored workspace is a new row (new host_id)
-- whose predecessor must be tombstoned (state = 'destroyed') first.
--
-- account_key_bundles: one row per user holding the argon2id KDF inputs and
-- the password-wrapped data-encryption key (also opaque to the server).
-- Present only while the user has a non-empty master password.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/013_workspace_sync.sql
--
-- Idempotent: rerunning is a no-op once the tables + indexes exist.

BEGIN;

CREATE TABLE IF NOT EXISTS workspace_records (
    user_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    color TEXT,
    provider_kind TEXT NOT NULL,
    hosting_device_id TEXT,
    device_label TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK (state IN ('active', 'destroyed')),
    restored_from_host_id TEXT,
    backup_kind TEXT NOT NULL CHECK (backup_kind IN ('imbue_r2', 'api_key', 'none')),
    encrypted_secrets BYTEA,
    revision INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, host_id)
);

CREATE INDEX IF NOT EXISTS workspace_records_user_idx ON workspace_records (user_id);

CREATE UNIQUE INDEX IF NOT EXISTS workspace_records_one_active_per_agent_idx
    ON workspace_records (user_id, agent_id)
    WHERE state = 'active';

CREATE TABLE IF NOT EXISTS account_key_bundles (
    user_id TEXT PRIMARY KEY,
    kdf_salt BYTEA NOT NULL,
    kdf_time_cost INTEGER NOT NULL,
    kdf_memory_kib INTEGER NOT NULL,
    kdf_parallelism INTEGER NOT NULL,
    wrapped_dek BYTEA NOT NULL,
    key_epoch INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMIT;
