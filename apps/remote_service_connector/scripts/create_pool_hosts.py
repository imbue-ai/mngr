#!/usr/bin/env python3
"""Create pre-provisioned Vultr pool hosts for the host pool feature.

For each host, this script:
  1. Runs `mngr create` to provision a VPS with a Docker container and agent
  2. Stops the agent (so it is ready for later assignment)
  3. Installs the management SSH public key on both the VPS and container
  4. Extracts host metadata from `mngr list --format json`
  5. Inserts a row into the pool_hosts database table

Usage:
    uv run python apps/remote_service_connector/scripts/create_pool_hosts.py \
        --count 3 --version v0.1.0 \
        --management-public-key-file ./management_key/id_ed25519.pub \
        --database-url $DATABASE_URL
"""

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

import click
import psycopg2
from loguru import logger

_DEFAULT_REGION: Final[str] = "ewr"
_DEFAULT_PLAN: Final[str] = "vc2-2c-4gb"
_CONTAINER_SSH_PORT: Final[int] = 2222
_MNGR_COMMAND_TIMEOUT_SECONDS: Final[int] = 600
_SSH_COMMAND_TIMEOUT_SECONDS: Final[int] = 60


def _run_mngr_command(
    args: list[str],
    timeout: int = _MNGR_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run a mngr CLI command via `uv run` and return the result."""
    full_command = ["uv", "run", "mngr"] + args
    logger.info("  Running: {}", " ".join(full_command))
    return subprocess.run(
        full_command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _get_agent_info(agent_name: str) -> dict[str, Any] | None:
    """Query `mngr list --format json` and find the agent by name."""
    result = _run_mngr_command(
        ["list", "--format", "json", "--include", f'name == "{agent_name}"'],
        timeout=60,
    )
    if result.returncode != 0:
        logger.warning("mngr list failed: {}", result.stderr)
        return None

    # mngr list --format json outputs one JSON object per line (jsonl-style) or a JSON array
    for line in result.stdout.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        # Parse the JSON output: it may be a list of agents or a single agent object
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("name") == agent_name:
                    return item
        elif isinstance(data, dict) and data.get("name") == agent_name:
            return data
        else:
            logger.debug("Skipped unrecognized JSON line: {}", stripped[:100])
    return None


def _extract_vps_ip(agent_info: dict[str, Any]) -> str | None:
    """Extract the VPS IP address from mngr agent info."""
    host = agent_info.get("host")
    if not isinstance(host, dict):
        return None
    ssh = host.get("ssh")
    if not isinstance(ssh, dict):
        return None
    host_value = ssh.get("host")
    if isinstance(host_value, str):
        return host_value
    return None


def _extract_ssh_key_path(agent_info: dict[str, Any]) -> str | None:
    """Extract the SSH key path from mngr agent info."""
    host = agent_info.get("host")
    if not isinstance(host, dict):
        return None
    ssh = host.get("ssh")
    if not isinstance(ssh, dict):
        return None
    key_path = ssh.get("key_path")
    if isinstance(key_path, str):
        return key_path
    return None


def _install_management_key_via_ssh(
    vps_ip: str,
    ssh_key_path: str,
    management_public_key: str,
    port: int,
    user: str,
) -> bool:
    """SSH into a host and append the management public key to authorized_keys."""
    key_line = management_public_key.strip()
    ssh_command = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=15",
        "-i",
        ssh_key_path,
        "-p",
        str(port),
        "{}@{}".format(user, vps_ip),
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo {} >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys".format(
            shlex.quote(key_line)
        ),
    ]
    logger.info("  Installing management key on {}@{}:{}", user, vps_ip, port)
    result = subprocess.run(
        ssh_command,
        capture_output=True,
        text=True,
        timeout=_SSH_COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        logger.warning("SSH key installation failed on {}:{}: {}", vps_ip, port, result.stderr)
        return False
    return True


def _install_management_key_via_mngr_exec(
    agent_name: str,
    management_public_key: str,
) -> bool:
    """Use `mngr exec` to install the management key inside the Docker container."""
    key_line = management_public_key.strip()
    command = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo {} >> ~/.ssh/authorized_keys && ".format(shlex.quote(key_line))
        + "chmod 600 ~/.ssh/authorized_keys"
    )
    logger.info("  Installing management key in container via mngr exec")
    result = _run_mngr_command(["exec", agent_name, command], timeout=60)
    if result.returncode != 0:
        logger.warning("mngr exec failed: {}", result.stderr)
        return False
    return True


def _insert_pool_host_row(
    database_url: str,
    vps_ip: str,
    vps_instance_id: str,
    agent_id: str,
    host_id: str,
    container_ssh_port: int,
    version: str,
) -> int:
    """Insert a row into the pool_hosts table and return the row ID."""
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pool_hosts "
                "(vps_ip, vps_instance_id, agent_id, host_id, ssh_port, ssh_user, container_ssh_port, status, version) "
                "VALUES (%s, %s, %s, %s, 22, 'root', %s, 'available', %s) "
                "RETURNING id",
                (vps_ip, vps_instance_id, agent_id, host_id, container_ssh_port, version),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                logger.error("INSERT did not return an id")
                sys.exit(1)
            row_id: int = row[0]
            conn.commit()
    finally:
        conn.close()
    return row_id


def _create_single_pool_host(
    host_idx: int,
    version: str,
    management_public_key: str,
    database_url: str,
    region: str,
    plan: str,
) -> bool:
    """Create a single pool host. Returns True on success, False on failure."""
    suffix = uuid4().hex
    host_name = f"pool-host-{suffix}"
    agent_name = f"pool-agent-{suffix}"
    address = f"{agent_name}@{host_name}.vultr"

    logger.info("[{}] Creating pool host: {}", host_idx, address)

    # Create the agent and host
    create_result = _run_mngr_command(
        [
            "create",
            address,
            "--no-connect",
            "--label",
            f"pool_version={version}",
            "--label",
            f"pool_region={region}",
            "--host-label",
            f"plan={plan}",
        ]
    )
    if create_result.returncode != 0:
        logger.error("mngr create failed: {}", create_result.stderr)
        return False

    logger.info("  Created agent: {}", agent_name)

    # Stop the agent
    stop_result = _run_mngr_command(["stop", agent_name, "--no-graceful"])
    if stop_result.returncode != 0:
        logger.warning("mngr stop failed (continuing): {}", stop_result.stderr)

    # Get agent info from mngr list
    agent_info = _get_agent_info(agent_name)
    if agent_info is None:
        logger.error("Could not find agent info for {}", agent_name)
        return False

    vps_ip = _extract_vps_ip(agent_info)
    if vps_ip is None:
        logger.error("Could not extract VPS IP from agent info")
        logger.error("Agent info: {}", json.dumps(agent_info, indent=2))
        return False

    agent_id = str(agent_info.get("id", ""))
    if not agent_id:
        logger.error("Could not extract agent_id from agent info")
        return False

    host = agent_info.get("host")
    host_id = ""
    if isinstance(host, dict):
        host_id = str(host.get("id", ""))

    if not host_id:
        logger.error("Could not extract host_id from agent info")
        return False

    # Use the host_id as a fallback for vps_instance_id
    vps_instance_id = host_id

    ssh_key_path = _extract_ssh_key_path(agent_info)
    if ssh_key_path is None:
        logger.warning("Could not extract SSH key path, skipping VPS key installation")
    else:
        # Install management key on the VPS
        _install_management_key_via_ssh(
            vps_ip=vps_ip,
            ssh_key_path=ssh_key_path,
            management_public_key=management_public_key,
            port=22,
            user="root",
        )

    # Install management key in the Docker container via mngr exec
    _install_management_key_via_mngr_exec(
        agent_name=agent_name,
        management_public_key=management_public_key,
    )

    # Insert row into pool_hosts table
    row_id = _insert_pool_host_row(
        database_url=database_url,
        vps_ip=vps_ip,
        vps_instance_id=vps_instance_id,
        agent_id=agent_id,
        host_id=host_id,
        container_ssh_port=_CONTAINER_SSH_PORT,
        version=version,
    )

    logger.info("  Inserted pool_hosts row id={} (agent_id={}, vps_ip={})", row_id, agent_id, vps_ip)
    return True


@click.command()
@click.option(
    "--count",
    required=True,
    type=int,
    help="Number of pool hosts to create",
)
@click.option(
    "--version",
    required=True,
    type=str,
    help="Version label for the pool hosts (e.g. v0.1.0)",
)
@click.option(
    "--management-public-key-file",
    required=True,
    type=click.Path(exists=True),
    help="Path to the management SSH public key file",
)
@click.option(
    "--database-url",
    required=True,
    type=str,
    envvar="DATABASE_URL",
    help="Neon PostgreSQL connection string (or set DATABASE_URL env var)",
)
@click.option(
    "--region",
    type=str,
    default=_DEFAULT_REGION,
    show_default=True,
    help="Vultr region code",
)
@click.option(
    "--plan",
    type=str,
    default=_DEFAULT_PLAN,
    show_default=True,
    help="Vultr plan identifier",
)
def create_pool_hosts(
    count: int,
    version: str,
    management_public_key_file: str,
    database_url: str,
    region: str,
    plan: str,
) -> None:
    management_public_key = Path(management_public_key_file).read_text().strip()
    if not management_public_key:
        logger.error("Management public key file is empty")
        sys.exit(1)

    logger.info(
        "Creating {} pool host(s) with version={}, region={}, plan={}",
        count,
        version,
        region,
        plan,
    )
    logger.info("Management public key: {}...", management_public_key[:40])

    success_count = 0
    failure_count = 0

    for i in range(1, count + 1):
        try:
            is_success = _create_single_pool_host(
                host_idx=i,
                version=version,
                management_public_key=management_public_key,
                database_url=database_url,
                region=region,
                plan=plan,
            )
        except (subprocess.SubprocessError, psycopg2.Error, OSError) as exc:
            logger.warning("[{}] Failed with error: {}", i, exc)
            is_success = False

        if is_success:
            success_count += 1
        else:
            failure_count += 1

    logger.info("Done. Created {}/{} hosts successfully.", success_count, count)
    if failure_count > 0:
        logger.warning("{} host(s) failed. Check the output above for details.", failure_count)
        sys.exit(1)


if __name__ == "__main__":
    create_pool_hosts()
