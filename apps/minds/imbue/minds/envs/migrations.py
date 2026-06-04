"""Pool-hosts SQL migration runner with a real ``schema_migrations`` tracking table.

Replaces the previous "replay every .sql with ``IF NOT EXISTS`` guards"
pattern. The schema_migrations table records ``(version, applied_at)``
per migration filename; the runner only applies files whose name is not
yet in the table. New migrations should NOT use ``IF NOT EXISTS``
guards -- the tracking table is the source of truth for which migrations
have already run.

Backwards-compat: on the first run against an existing database that
was migrated under the old "replay-everything" pattern, the
schema_migrations table doesn't exist but the schema does. The runner
creates the table and then attempts every on-disk file in order; because
the existing files all use ``IF NOT EXISTS`` guards, replaying them
against the already-migrated database is a no-op (and gets recorded as
applied). New (post-this-change) migrations don't carry guards, so a
missing record means the migration has genuinely never run + needs to.

Shells out to ``psql`` for every operation (DDL + reads + applies)
rather than pulling in psycopg2; matches the existing
``neon_db.wipe_neon_db_schema`` pattern and avoids a new transitive
dependency.
"""

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import info_span
from imbue.minds.envs.providers.neon_db import NeonProviderError

# psql shellout timeout per migration. Generous enough to absorb a slow
# Neon cold-start; short enough that a real connectivity failure surfaces
# in well under a minute.
_PSQL_TIMEOUT_SECONDS: Final[float] = 60.0

# Tracking-table DDL. ``version`` is the literal SQL filename (e.g.
# ``001_attributes_jsonb.sql``); ``applied_at`` is the UTC timestamp
# the runner finished applying it.
_SCHEMA_MIGRATIONS_DDL: Final[str] = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "    version TEXT PRIMARY KEY,"
    "    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
    ")"
)


class MigrationRunnerError(NeonProviderError):
    """Raised when the schema_migrations runner fails to apply a file."""


def _psql_path() -> str:
    """Return the absolute path to ``psql`` or raise with operator-facing guidance."""
    path = shutil.which("psql")
    if path is None:
        raise MigrationRunnerError(
            "psql binary not on PATH; cannot run schema migrations. Install via "
            "`apt install postgresql-client` (Debian/Ubuntu) or `brew install libpq` (macOS)."
        )
    return path


def _list_migration_files(migrations_dir: Path) -> Sequence[Path]:
    """Return all ``.sql`` files in ``migrations_dir`` sorted lexicographically.

    The filenames are the source of truth for migration ordering (the
    repo convention is ``NNN_<name>.sql``, where ``NNN`` is a
    zero-padded integer prefix). Lex sort == apply order.
    """
    if not migrations_dir.is_dir():
        raise MigrationRunnerError(
            f"Migrations directory not found: {migrations_dir}. "
            "`minds env deploy` must be run from a checkout of the monorepo."
        )
    return sorted(migrations_dir.glob("*.sql"))


def _run_psql_command(dsn: SecretStr, *, sql: str, parent_cg: ConcurrencyGroup, cg_name: str) -> str:
    """Run a single SQL statement via ``psql -c`` and return stdout.

    Used for the small DDL + read + insert operations that don't warrant
    a separate .sql file. Raises :class:`MigrationRunnerError` on
    non-zero exit. ``ON_ERROR_STOP=1`` matches the existing pattern in
    :func:`apply_pool_hosts_migrations`.
    """
    command = [
        _psql_path(),
        dsn.get_secret_value(),
        "-v",
        "ON_ERROR_STOP=1",
        "-t",
        "-A",
        "-c",
        sql,
    ]
    cg = parent_cg.make_concurrency_group(name=cg_name)
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_PSQL_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise MigrationRunnerError(f"`psql -c {sql!r}` exited {result.returncode}: {stderr}")
    return result.stdout


def ensure_schema_migrations_table(dsn: SecretStr, *, parent_cg: ConcurrencyGroup) -> None:
    """Create the schema_migrations tracking table if it doesn't already exist.

    Idempotent: running against a database that already has the table
    is a no-op (the DDL itself uses ``IF NOT EXISTS``).
    """
    _run_psql_command(
        dsn,
        sql=_SCHEMA_MIGRATIONS_DDL,
        parent_cg=parent_cg,
        cg_name="psql-ensure-schema-migrations-table",
    )


def _list_applied_versions(dsn: SecretStr, *, parent_cg: ConcurrencyGroup) -> set[str]:
    """Return the set of ``version`` strings already recorded in schema_migrations.

    Parses psql's ``-t -A`` (tuples-only, unaligned) output: one row per
    line, no header / separator clutter.
    """
    stdout = _run_psql_command(
        dsn,
        sql="SELECT version FROM schema_migrations",
        parent_cg=parent_cg,
        cg_name="psql-list-applied-versions",
    )
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def _record_applied_version(dsn: SecretStr, version: str, *, parent_cg: ConcurrencyGroup) -> None:
    """Insert a row into schema_migrations marking ``version`` as applied.

    ``ON CONFLICT DO NOTHING`` so concurrent deploys (which shouldn't
    happen but might) don't break the runner. Embedded in a quoted SQL
    literal via single-quote escaping; ``version`` is a filename under
    the operator's control + comes from sorted Path.glob results, so
    it can't contain SQL-injection material in practice -- but we
    still escape single quotes defensively.
    """
    safe_version = version.replace("'", "''")
    sql = f"INSERT INTO schema_migrations (version) VALUES ('{safe_version}') ON CONFLICT (version) DO NOTHING"
    _run_psql_command(
        dsn,
        sql=sql,
        parent_cg=parent_cg,
        cg_name=f"psql-record-migration-{version}",
    )


def list_pending_pool_hosts_migrations(
    dsn: SecretStr, *, migrations_dir: Path, parent_cg: ConcurrencyGroup
) -> list[Path]:
    """Return on-disk migration files whose ``version`` is not yet in schema_migrations.

    Used by deploy.py both to decide whether a deploy is "applying a
    migration" (any non-empty return) and as the input to
    :func:`apply_pool_hosts_migrations`.
    """
    ensure_schema_migrations_table(dsn, parent_cg=parent_cg)
    applied = _list_applied_versions(dsn, parent_cg=parent_cg)
    on_disk = _list_migration_files(migrations_dir)
    return [path for path in on_disk if path.name not in applied]


def apply_pool_hosts_migrations(
    dsn: SecretStr, *, migrations_dir: Path, parent_cg: ConcurrencyGroup
) -> tuple[Path, ...]:
    """Apply every pending migration in order, recording each in schema_migrations.

    Shells out to ``psql`` per file. Returns the tuple of files actually
    applied this run, in apply order -- useful for the deploy log to
    surface what just ran.

    Backwards-compat note: on the first deploy against an existing
    database that was migrated under the old replay-runner, the
    schema_migrations table is empty but the schema is in place. The
    existing migration files all use ``IF NOT EXISTS`` guards, so
    re-applying them is a no-op (and we record the row).
    """
    psql = _psql_path()
    pending = list_pending_pool_hosts_migrations(dsn, migrations_dir=migrations_dir, parent_cg=parent_cg)
    if not pending:
        logger.info("schema_migrations: no pending migrations to apply.")
        return ()

    applied: list[Path] = []
    for migration in pending:
        with info_span("Applying pool-hosts migration {!r}", migration.name):
            command = [
                psql,
                dsn.get_secret_value(),
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                str(migration),
            ]
            cg = parent_cg.make_concurrency_group(name=f"psql-pool-migration-{migration.stem}")
            with cg:
                result = cg.run_process_to_completion(
                    command=command,
                    timeout=_PSQL_TIMEOUT_SECONDS,
                    is_checked_after=False,
                )
            if result.returncode != 0:
                stderr = result.stderr.strip() or result.stdout.strip()
                raise MigrationRunnerError(
                    f"`psql` exited {result.returncode} while applying {migration.name}: {stderr}"
                )
            _record_applied_version(dsn, migration.name, parent_cg=parent_cg)
            applied.append(migration)
    return tuple(applied)


def seed_paid_list_defaults(
    dsn: SecretStr,
    *,
    domains: Sequence[str],
    emails: Sequence[str],
    parent_cg: ConcurrencyGroup,
) -> tuple[str, ...]:
    """Seed-if-absent default paid domains / emails into the host_pool DB.

    For each value, runs ``INSERT INTO <table> (<col>) VALUES (...) ON CONFLICT
    (<col>) DO NOTHING`` so the row is created (with the table's ``is_paid=true``
    default) only when absent -- it sets the tier's initial default but never
    re-activates an entry an operator soft-removed. Values are lowercased to
    match the connector's normalized lookups. Returns the lowercased values
    that were seed-attempted, in ``domains`` then ``emails`` order.

    Must run AFTER :func:`apply_pool_hosts_migrations` (the tables must exist).
    A no-op when both lists are empty.
    """
    seeded: list[str] = []
    for table, column, values in (("paid_domains", "domain", domains), ("paid_emails", "email", emails)):
        for index, raw in enumerate(values):
            value = raw.strip().lower()
            if not value:
                continue
            # ``value`` comes from a committed deploy.toml (operator-controlled),
            # but escape single quotes defensively -- same approach as
            # ``_record_applied_version``.
            safe_value = value.replace("'", "''")
            sql = f"INSERT INTO {table} ({column}) VALUES ('{safe_value}') ON CONFLICT ({column}) DO NOTHING"
            _run_psql_command(dsn, sql=sql, parent_cg=parent_cg, cg_name=f"psql-seed-{table}-{index}")
            seeded.append(value)
    return tuple(seeded)
