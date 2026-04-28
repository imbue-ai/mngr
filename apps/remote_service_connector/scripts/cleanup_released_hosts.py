#!/usr/bin/env python3
"""Clean up released pool hosts by destroying them via mngr and removing DB rows.

Reads all rows with status='released' from the pool_hosts table, runs
`mngr destroy` on each, and deletes the DB row on success.

Usage:
    uv run python apps/remote_service_connector/scripts/cleanup_released_hosts.py \
        --database-url $DATABASE_URL
"""

import subprocess
import sys
from typing import Final

import click
import psycopg2
from loguru import logger

_MNGR_COMMAND_TIMEOUT_SECONDS: Final[int] = 300


def _run_mngr_destroy(agent_id: str) -> subprocess.CompletedProcess[str]:
    """Run `mngr destroy` for the given agent_id."""
    full_command = ["uv", "run", "mngr", "destroy", agent_id, "--force"]
    logger.info("  Running: {}", " ".join(full_command))
    return subprocess.run(
        full_command,
        capture_output=True,
        text=True,
        timeout=_MNGR_COMMAND_TIMEOUT_SECONDS,
    )


@click.command()
@click.option(
    "--database-url",
    required=True,
    type=str,
    envvar="DATABASE_URL",
    help="Neon PostgreSQL connection string (or set DATABASE_URL env var)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="List released hosts without destroying them",
)
def cleanup_released_hosts(database_url: str, dry_run: bool) -> None:
    conn = psycopg2.connect(database_url)

    # Fetch all released hosts
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, agent_id, host_id, vps_ip FROM pool_hosts WHERE status = 'released'")
            released_rows = cur.fetchall()
    except psycopg2.Error:
        conn.close()
        raise

    if not released_rows:
        logger.info("No released hosts found.")
        conn.close()
        return

    logger.info("Found {} released host(s).", len(released_rows))
    if dry_run:
        for row in released_rows:
            db_id, agent_id, host_id, vps_ip = row
            logger.info(
                "  id={} agent_id={} host_id={} vps_ip={}",
                db_id,
                agent_id,
                host_id,
                vps_ip,
            )
        conn.close()
        return

    success_count = 0
    failure_count = 0

    for row in released_rows:
        db_id, agent_id, host_id, vps_ip = row
        logger.info("Destroying host id={} agent_id={} vps_ip={}", db_id, agent_id, vps_ip)

        try:
            result = _run_mngr_destroy(agent_id)
        except subprocess.TimeoutExpired:
            logger.warning("mngr destroy timed out for agent_id={}", agent_id)
            failure_count += 1
            continue

        if result.returncode != 0:
            logger.warning("mngr destroy failed for agent_id={}: {}", agent_id, result.stderr)
            failure_count += 1
            continue

        # Delete the DB row on successful destruction
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pool_hosts WHERE id = %s", (db_id,))
                conn.commit()
            logger.info("  Deleted pool_hosts row id={}", db_id)
            success_count += 1
        except psycopg2.Error as exc:
            logger.warning("Failed to delete DB row id={}: {}", db_id, exc)
            conn.rollback()
            failure_count += 1

    conn.close()

    logger.info(
        "Done. Cleaned up {}/{} hosts successfully.",
        success_count,
        len(released_rows),
    )
    if failure_count > 0:
        logger.warning("{} host(s) failed. Check the output above for details.", failure_count)
        sys.exit(1)


if __name__ == "__main__":
    cleanup_released_hosts()
