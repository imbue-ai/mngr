-- Migration 005: paid_domains + paid_emails tables.
--
-- Replaces the ``PAID_ACCOUNT_SUFFIXES`` env-var allowlist. A user counts
-- as "paid" when their verified email is matched by a row in either table
-- with ``is_paid = true``:
--   * ``paid_emails``  -- exact full-email match (e.g. ``bob@gmail.com``).
--   * ``paid_domains`` -- exact domain match on the part after ``@`` (e.g.
--     ``imbue.com`` matches ``alice@imbue.com`` but NOT ``alice@eng.imbue.com``).
--
-- Both ``domain`` and ``email`` are stored lowercased (normalized by the
-- connector on write); the primary key enforces uniqueness.
--
-- Rows are never hard-deleted. "Removing" an entry flips ``is_paid`` to
-- false and bumps ``updated_at`` so we retain history of when an account
-- or company stopped paying (useful for later reclaiming resources).
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/005_paid_lists.sql
--
-- NOT idempotent on its own: the schema_migrations runner
-- (apps/minds/imbue/minds/envs/migrations.py) records this filename once
-- applied and never re-runs it, so this migration deliberately omits the
-- ``IF NOT EXISTS`` guards the pre-runner migrations (000-004) carried.

BEGIN;

CREATE TABLE paid_domains (
    domain TEXT PRIMARY KEY,
    is_paid BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE paid_emails (
    email TEXT PRIMARY KEY,
    is_paid BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMIT;
