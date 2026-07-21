-- Migration 015: R2 storage-cleanup grants.
--
-- One row per cleanup grant: a temporary restore of an over-quota account's
-- sweep-downgraded bucket keys so client-side restic cleanup (forget +
-- prune, which needs full write access) can run. ``baseline_bytes`` is the
-- live REST-measured usage at grant time; settlement (an explicit
-- /account/storage-recheck, or the hourly sweep once ``expires_at`` passes)
-- records ``settled_bytes`` and whether usage decreased at all. Grants that
-- settle without any decrease count against a rolling failed-grant budget
-- enforced in code (module constants in app.py).
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/015_r2_cleanup_grants.sql
--
-- NOT idempotent on its own: the schema_migrations runner
-- (apps/minds/imbue/minds/envs/migrations.py) records this filename once
-- applied and never re-runs it, so this migration deliberately omits
-- ``IF NOT EXISTS`` guards.

BEGIN;

CREATE TABLE r2_cleanup_grants (
    grant_id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    username_prefix TEXT NOT NULL,
    baseline_bytes BIGINT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    settled_at TIMESTAMPTZ,
    settled_bytes BIGINT,
    is_decreased BOOLEAN
);

-- The failed-grant budget counts an account's settled-without-decrease
-- grants inside a rolling window.
CREATE INDEX r2_cleanup_grants_user_id_granted_at_idx
    ON r2_cleanup_grants (user_id, granted_at);

-- The sweep's expiry-settlement fallback scans unsettled grants only.
CREATE INDEX r2_cleanup_grants_unsettled_idx
    ON r2_cleanup_grants (expires_at) WHERE settled_at IS NULL;

COMMIT;
