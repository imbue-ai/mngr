-- Migration 016: rename the misnamed ``username_prefix`` columns to
-- ``user_id_prefix``.
--
-- The value was never a username: it is the 16-hex prefix of the SuperTokens
-- user id, used to namespace tunnels / leases / buckets. The Python code was
-- renamed to ``user_id_prefix`` first (translating at the row boundary); this
-- migration brings the ``account_entitlements`` and ``r2_cleanup_grants``
-- columns (and the entitlements prefix index) in line so code and schema use
-- one name again.
--
-- Deploy ordering note: ``minds env deploy`` applies migrations before the
-- new connector code rolls out, so connector containers still running the
-- pre-rename code would fail these columns' queries during that brief
-- window. Quota reads/writes and cleanup grants are the only affected paths;
-- they recover as soon as the new code is live. A ``modal app rollback`` to a
-- pre-rename deploy requires manually renaming the columns back.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/016_rename_username_prefix.sql
--
-- NOT idempotent on its own: the schema_migrations runner
-- (apps/minds/imbue/minds/envs/migrations.py) records this filename once
-- applied and never re-runs it.

BEGIN;

ALTER TABLE account_entitlements RENAME COLUMN username_prefix TO user_id_prefix;
ALTER INDEX account_entitlements_username_prefix_idx RENAME TO account_entitlements_user_id_prefix_idx;

ALTER TABLE r2_cleanup_grants RENAME COLUMN username_prefix TO user_id_prefix;

COMMIT;
