-- Migration 014: account plans + per-user entitlements, and R2 key
-- enforcement state.
--
-- ``plans`` holds the git-owned plan definitions (explorer / ally today).
-- Rows are written (overwriting) from each tier's deploy.toml on every
-- ``minds env deploy``; the connector only reads them. There is no FK from
-- account_entitlements.plan_name to plans on purpose -- a user row must not
-- break if a plan is renamed or removed from deploy.toml.
--
-- ``account_entitlements`` is one row per account, keyed by the full
-- SuperTokens user_id. ``username_prefix`` (the 16-hex prefix used to
-- namespace tunnels / leases / buckets) is indexed so agent-auth paths --
-- which only know the prefix from the tunnel name -- can resolve the row.
-- The quota columns are copied wholesale from the plan at assignment and
-- are the operator-adjustable source of truth thereafter; changing a plan's
-- defaults never retroactively changes existing rows.
--
-- ``r2_keys.enforced_access`` records the storage-quota sweep's enforcement
-- state: NULL means the Cloudflare token's live policy matches the intended
-- ``access`` column; 'read' means the sweep downgraded a readwrite key
-- because the owner is over their storage quota (and should restore it once
-- they drop back under).
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/014_plans_entitlements.sql
--
-- NOT idempotent on its own: the schema_migrations runner
-- (apps/minds/imbue/minds/envs/migrations.py) records this filename once
-- applied and never re-runs it, so this migration deliberately omits
-- ``IF NOT EXISTS`` guards.

BEGIN;

CREATE TABLE plans (
    plan_name TEXT PRIMARY KEY,
    max_remote_workspaces INTEGER NOT NULL,
    max_tunnels INTEGER NOT NULL,
    max_services_per_tunnel INTEGER NOT NULL,
    max_buckets INTEGER NOT NULL,
    max_total_bucket_bytes BIGINT NOT NULL,
    monthly_llm_spend_usd NUMERIC(12, 2) NOT NULL,
    max_active_synced_workspaces INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE account_entitlements (
    user_id TEXT PRIMARY KEY,
    username_prefix TEXT NOT NULL,
    plan_name TEXT NOT NULL,
    max_remote_workspaces INTEGER NOT NULL,
    max_tunnels INTEGER NOT NULL,
    max_services_per_tunnel INTEGER NOT NULL,
    max_buckets INTEGER NOT NULL,
    max_total_bucket_bytes BIGINT NOT NULL,
    monthly_llm_spend_usd NUMERIC(12, 2) NOT NULL,
    max_active_synced_workspaces INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX account_entitlements_username_prefix_idx
    ON account_entitlements (username_prefix);

ALTER TABLE r2_keys ADD COLUMN enforced_access TEXT
    CHECK (enforced_access IS NULL OR enforced_access IN ('read', 'readwrite'));

COMMIT;
