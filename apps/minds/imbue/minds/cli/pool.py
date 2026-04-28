"""CLI command to provision pool hosts for LEASED mode.

Creates Vultr VPS hosts using the same mngr create flow as CLOUD mode,
then stops the agent, installs a management SSH key, opens the container
SSH port in UFW, and registers the host in the pool database.
"""

import json
import shlex
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

import click
import psycopg2
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.minds.desktop_client.api_key_store import generate_api_key

_CONTAINER_SSH_PORT: Final[int] = 2222
_MNGR_COMMAND_TIMEOUT_SECONDS: Final[int] = 600

_RSYNC_EXCLUDES: Final[tuple[str, ...]] = (
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    ".test_output",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "uv.lock",
    ".external_worktrees",
)
_SSH_COMMAND_TIMEOUT_SECONDS: Final[int] = 60


def _run_mngr_command(
    args: list[str],
    cwd: Path | None = None,
    timeout: int = _MNGR_COMMAND_TIMEOUT_SECONDS,
) -> FinishedProcess:
    """Run a mngr CLI command and return the result."""
    full_command = ["mngr"] + args
    logger.info("  Running: {}", " ".join(full_command))
    cg = ConcurrencyGroup(name="pool-mngr")
    with cg:
        return cg.run_process_to_completion(
            command=full_command,
            timeout=float(timeout),
            is_checked_after=False,
            cwd=cwd,
        )


def _run_ssh_command(
    vps_ip: str,
    ssh_key_path: str,
    port: int,
    command: str,
) -> bool:
    """Run a command on a host via SSH. Returns True on success."""
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
        "root@{}".format(vps_ip),
        command,
    ]
    logger.info("  SSH {}:{}: {}", vps_ip, port, command)
    cg = ConcurrencyGroup(name="pool-ssh")
    with cg:
        result = cg.run_process_to_completion(
            command=ssh_command,
            timeout=float(_SSH_COMMAND_TIMEOUT_SECONDS),
            is_checked_after=False,
        )
    if result.returncode != 0:
        logger.warning("SSH command failed: {}", result.stderr.strip())
        return False
    return True


def _get_agent_info(agent_name: str) -> dict[str, Any] | None:
    """Query mngr list --format json and find the agent by name."""
    result = _run_mngr_command(
        ["list", "--format", "json", "--include", 'name == "{}"'.format(agent_name)],
        timeout=60,
    )
    if result.returncode != 0:
        logger.warning("mngr list failed: {}", result.stderr)
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("Failed to parse mngr list output")
        return None

    agents: list[dict[str, Any]] = []
    if isinstance(data, dict) and "agents" in data:
        agents = data["agents"]
    elif isinstance(data, list):
        agents = data
    else:
        return None

    for agent in agents:
        if isinstance(agent, dict) and agent.get("name") == agent_name:
            return agent
    return None


def _sync_mngr_into_template(mngr_source: Path, workspace_dir: Path) -> None:
    """Rsync the mngr monorepo into the template's vendor/mngr/ directory."""
    vendor_mngr = workspace_dir / "vendor" / "mngr"
    vendor_mngr.mkdir(parents=True, exist_ok=True)
    exclude_args: list[str] = []
    for pattern in _RSYNC_EXCLUDES:
        exclude_args.extend(["--exclude", pattern])
    command = (
        ["rsync", "-a", "--delete"]
        + exclude_args
        + [
            "{}/".format(mngr_source),
            "{}/".format(vendor_mngr),
        ]
    )
    logger.info("Syncing mngr source into {}", vendor_mngr)
    cg = ConcurrencyGroup(name="rsync-vendor")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            is_checked_after=False,
            timeout=120.0,
        )
    if result.returncode != 0:
        logger.warning("rsync failed (exit {}): {}", result.returncode, result.stderr.strip())


def _create_single_pool_host(
    workspace_dir: Path,
    version: str,
    management_public_key: str,
    database_url: str,
) -> bool:
    """Create a single pool host. Returns True on success."""
    suffix = uuid4().hex
    agent_name = "pool-{}".format(suffix)
    host_name = "{}-host".format(agent_name)
    address = "{}@{}.vultr".format(agent_name, host_name)

    logger.info("Creating pool host: {}", address)

    api_key = generate_api_key()
    mngr_command = [
        "create",
        address,
        "--new-host",
        "--no-connect",
        "--idle-mode",
        "disabled",
        "--template",
        "main",
        "--template",
        "vultr",
        "--env",
        "MINDS_API_KEY={}".format(api_key),
        "--label",
        "workspace={}".format(agent_name),
        "--label",
        "user_created=true",
        "--label",
        "is_primary=true",
        "--label",
        "pool_version={}".format(version),
    ]
    mngr_command.extend(
        [
            "--host-env",
            "MNGR_HOST_DIR=/mngr",
            "--pass-host-env",
            "MNGR_PREFIX",
        ]
    )

    create_result = _run_mngr_command(mngr_command, cwd=workspace_dir)
    if create_result.returncode != 0:
        logger.error("mngr create failed: {}", create_result.stderr)
        return False

    logger.info("  Created agent: {}", agent_name)

    # Stop the agent but keep the container running
    stop_result = _run_mngr_command(["stop", agent_name])
    if stop_result.returncode != 0:
        logger.warning("mngr stop failed (continuing): {}", stop_result.stderr)

    # Ensure sshd stays running in the container
    logger.info("  Ensuring sshd is running in container")
    _run_mngr_command(["exec", agent_name, "/usr/sbin/sshd"], timeout=30)

    # Get agent info
    agent_info = _get_agent_info(agent_name)
    if agent_info is None:
        logger.error("Could not find agent info for {}", agent_name)
        return False

    host = agent_info.get("host")
    if not isinstance(host, dict):
        logger.error("No host info in agent data")
        return False

    ssh = host.get("ssh")
    if not isinstance(ssh, dict):
        logger.error("No SSH info in host data")
        return False

    vps_ip = ssh.get("host")
    if not isinstance(vps_ip, str):
        logger.error("No VPS IP in SSH info")
        return False

    container_key_path = ssh.get("key_path")
    if not isinstance(container_key_path, str):
        logger.error("No SSH key path in host data")
        return False

    agent_id = str(agent_info.get("id", ""))
    host_id = str(host.get("id", ""))
    if not agent_id or not host_id:
        logger.error("Missing agent_id or host_id")
        return False

    # Derive the VPS SSH key path from the container key path
    vps_key_path = str(Path(container_key_path).parent / "vps_ssh_key")

    # Open the container SSH port in UFW
    _run_ssh_command(vps_ip, vps_key_path, 22, "ufw allow {}/tcp".format(_CONTAINER_SSH_PORT))

    # Install management key on VPS
    key_line = shlex.quote(management_public_key.strip())
    install_cmd = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo {} >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys".format(
        key_line
    )
    _run_ssh_command(vps_ip, vps_key_path, 22, install_cmd)

    # Install management key in container
    logger.info("  Installing management key in container via mngr exec")
    _run_mngr_command(["exec", agent_name, install_cmd], timeout=60)

    # Insert into pool_hosts table
    row_id = uuid4()
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pool_hosts "
                "(id, vps_ip, vps_instance_id, agent_id, host_id, ssh_port, ssh_user, "
                "container_ssh_port, status, version, created_at) "
                "VALUES (%s, %s, %s, %s, %s, 22, 'root', %s, 'available', %s, NOW()) "
                "RETURNING id",
                (str(row_id), vps_ip, host_id, agent_id, host_id, _CONTAINER_SSH_PORT, version),
            )
            conn.commit()
    finally:
        conn.close()

    logger.info("  Pool host ready: id={}, agent_id={}, vps_ip={}", row_id, agent_id, vps_ip)
    return True


@click.group()
def pool() -> None:
    """Manage the pre-provisioned host pool for LEASED mode."""


@pool.command("create")
@click.option("--count", required=True, type=int, help="Number of pool hosts to create")
@click.option("--version", required=True, type=str, help="Version label (e.g. v0.1.0 or branch name)")
@click.option(
    "--workspace-dir", required=True, type=click.Path(exists=True), help="Path to the template repo checkout"
)
@click.option(
    "--management-public-key-file",
    required=True,
    type=click.Path(exists=True),
    help="Path to the management SSH public key",
)
@click.option(
    "--database-url", required=True, type=str, envvar="DATABASE_URL", help="Neon PostgreSQL direct connection string"
)
@click.option(
    "--mngr-source",
    type=click.Path(exists=True),
    default=None,
    help="Path to the mngr monorepo root. If provided, rsyncs into the template's vendor/mngr/ before creating hosts.",
)
def pool_create(
    count: int,
    version: str,
    workspace_dir: str,
    management_public_key_file: str,
    database_url: str,
    mngr_source: str | None,
) -> None:
    """Create pre-provisioned pool hosts for LEASED mode."""
    management_public_key = Path(management_public_key_file).read_text().strip()
    if not management_public_key:
        logger.error("Management public key file is empty")
        raise SystemExit(1)

    workspace_path = Path(workspace_dir)
    if mngr_source is not None:
        _sync_mngr_into_template(Path(mngr_source), workspace_path)

    logger.info("Creating {} pool host(s) with version={}", count, version)

    success_count = 0
    for i in range(1, count + 1):
        logger.info("[{}/{}]", i, count)
        try:
            is_success = _create_single_pool_host(
                workspace_dir=workspace_path,
                version=version,
                management_public_key=management_public_key,
                database_url=database_url,
            )
        except (ConcurrencyGroupError, psycopg2.Error, OSError) as exc:
            logger.warning("[{}] Failed: {}", i, exc)
            is_success = False

        if is_success:
            success_count += 1

    logger.info("Done. Created {}/{} hosts.", success_count, count)
    if success_count < count:
        raise SystemExit(1)
