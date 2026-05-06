"""`mngr imbue_cloud admin pool ...` -- operator-only pool provisioning.

Ported from ``apps/minds/imbue/minds/cli/pool.py``. Provisions Vultr VPSes via
``mngr create`` (the imbue-team operator must have a Vultr-configured mngr
provider available locally), waits for the agent, installs a management SSH key
on both the VPS and the container, then writes a row to the connector's Neon
``pool_hosts`` table.

Authentication: this command talks to Neon directly via ``DATABASE_URL`` and to
Vultr via the operator's local ``mngr`` provider config. It does NOT use the
operator's SuperTokens session; the connector is not involved in pool
provisioning at all.
"""

import json as _json
import shlex
import sys
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
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json

_CONTAINER_SSH_PORT: Final[int] = 2222
# 30 min: the inner ``mngr create ... --template vultr`` builds a fresh
# Docker image on the leased VPS, which can take 10-20 min (network bound).
# A previous 10-min cap occasionally killed otherwise-healthy provisions.
_MNGR_COMMAND_TIMEOUT_SECONDS: Final[int] = 1800
_SSH_COMMAND_TIMEOUT_SECONDS: Final[int] = 60

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


@click.group(name="admin")
def admin() -> None:
    """Operator-only commands."""


@admin.group(name="pool")
def pool() -> None:
    """Pool host provisioning (Vultr + Neon)."""


def _stream_subprocess_line(line: str, is_stdout: bool) -> None:
    """Mirror a child-process line to our stderr in real time.

    Used as the ``on_output`` callback for streaming ``mngr`` invocations
    so a multi-minute pool-host bake isn't a silent black box. The child
    mngr already routes its own ``logger.*`` traffic to its events.jsonl;
    this surfaces the same lines (plus any plain stdout/stderr writes)
    in the parent's terminal as the bake progresses.
    """
    suffix = "" if line.endswith("\n") else "\n"
    sys.stderr.write(line + suffix)
    sys.stderr.flush()


def _run_mngr_command(
    args: list[str],
    cwd: Path | None = None,
    timeout: int = _MNGR_COMMAND_TIMEOUT_SECONDS,
    is_streaming: bool = False,
) -> FinishedProcess:
    """Run a mngr CLI command and return the result.

    When ``is_streaming=True`` the child's stdout and stderr are mirrored
    to our stderr line-by-line via ``_stream_subprocess_line`` (and still
    captured in the returned ``FinishedProcess``). Use this for the
    inner ``mngr create`` during pool baking -- the run takes 8-15
    minutes and otherwise produces no visible output until completion,
    which makes diagnosing pool-bake failures (or just confirming that
    provisioning is making progress) difficult.
    """
    full_command = ["mngr"] + args
    logger.info("  Running: {}", " ".join(full_command))
    on_output = _stream_subprocess_line if is_streaming else None
    cg = ConcurrencyGroup(name="pool-mngr")
    with cg:
        return cg.run_process_to_completion(
            command=full_command,
            timeout=float(timeout),
            is_checked_after=False,
            cwd=cwd,
            on_output=on_output,
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
        f"root@{vps_ip}",
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
        ["list", "--format", "json", "--include", f'name == "{agent_name}"'],
        timeout=60,
    )
    if result.returncode != 0:
        logger.warning("mngr list failed: {}", result.stderr)
        return None

    try:
        data = _json.loads(result.stdout)
    except _json.JSONDecodeError:
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
            f"{mngr_source}/",
            f"{vendor_mngr}/",
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
    attributes: dict[str, Any],
    management_public_key: str,
    database_url: str,
) -> bool:
    """Create a single pool host. Returns True on success.

    Inserts a row with the request-side ``attributes`` dict so the connector's
    ``attributes @>`` match can find it.
    """
    suffix = uuid4().hex
    agent_name = f"pool-{suffix}"
    host_name = f"{agent_name}-host"
    address = f"{agent_name}@{host_name}.vultr"

    logger.info("Creating pool host: {}", address)

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
        "--label",
        f"workspace={agent_name}",
        "--label",
        "user_created=true",
        "--label",
        "is_primary=true",
        "--label",
        f"pool_attributes={_json.dumps(attributes)}",
        "--host-env",
        "MNGR_HOST_DIR=/mngr",
        "--pass-host-env",
        "MNGR_PREFIX",
    ]

    create_result = _run_mngr_command(mngr_command, cwd=workspace_dir, is_streaming=True)
    if create_result.returncode != 0:
        logger.error("mngr create failed: {}", create_result.stderr)
        return False

    logger.info("  Created agent: {}", agent_name)

    stop_result = _run_mngr_command(["stop", agent_name])
    if stop_result.returncode != 0:
        logger.warning("mngr stop failed (continuing): {}", stop_result.stderr)

    logger.info("  Ensuring sshd is running in container")
    # Match the cloud-init bump we apply to the host VPS (and the lima
    # provider's sshd config): the default ``MaxStartups=10:30:100``
    # caps the pre-auth queue tightly, and the imbue_cloud lease + claim
    # flow plus parallel ``mngr observe`` discovery routinely exceeds it
    # and loses connections mid-rsync.
    _run_mngr_command(
        [
            "exec",
            agent_name,
            "/usr/sbin/sshd",
            "-o",
            "MaxSessions=100",
            "-o",
            "MaxStartups=100:30:200",
        ],
        timeout=30,
    )

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

    vps_key_path = str(Path(container_key_path).parent / "vps_ssh_key")

    _run_ssh_command(vps_ip, vps_key_path, 22, f"ufw allow {_CONTAINER_SSH_PORT}/tcp")

    key_line = shlex.quote(management_public_key.strip())
    install_cmd = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo "
        + key_line
        + " >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    )
    _run_ssh_command(vps_ip, vps_key_path, 22, install_cmd)

    logger.info("  Installing management key in container via mngr exec")
    _run_mngr_command(["exec", agent_name, install_cmd], timeout=60)

    row_id = uuid4()
    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pool_hosts "
                    "(id, vps_ip, vps_instance_id, agent_id, host_id, ssh_port, ssh_user, "
                    "container_ssh_port, status, attributes, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, 22, 'root', %s, 'available', %s::jsonb, NOW())",
                    (
                        str(row_id),
                        vps_ip,
                        host_id,
                        agent_id,
                        host_id,
                        _CONTAINER_SSH_PORT,
                        _json.dumps(attributes),
                    ),
                )
    finally:
        conn.close()

    logger.info("  Pool host ready: id={}, agent_id={}, vps_ip={}", row_id, agent_id, vps_ip)
    return True


@pool.command(name="create")
@click.option("--count", required=True, type=int, help="Number of pool hosts to create")
@click.option(
    "--attributes",
    "attributes_json",
    required=True,
    help='Lease-attributes JSON for the new pool rows (e.g. \'{"version":"v1.2.3","cpus":2,"memory_gb":4}\')',
)
@click.option(
    "--workspace-dir",
    required=True,
    type=click.Path(exists=True),
    help="Path to the template repo checkout",
)
@click.option(
    "--management-public-key-file",
    required=True,
    type=click.Path(exists=True),
    help="Path to the management SSH public key",
)
@click.option(
    "--database-url",
    required=True,
    type=str,
    envvar="DATABASE_URL",
    help="Neon PostgreSQL direct connection string",
)
@click.option(
    "--mngr-source",
    type=click.Path(exists=True),
    default=None,
    help="Path to the mngr monorepo root. If provided, rsyncs into the template's vendor/mngr/ before creating hosts.",
)
def pool_create(
    count: int,
    attributes_json: str,
    workspace_dir: str,
    management_public_key_file: str,
    database_url: str,
    mngr_source: str | None,
) -> None:
    """Create pre-provisioned pool hosts."""
    try:
        parsed_attributes = _json.loads(attributes_json)
    except _json.JSONDecodeError as exc:
        logger.error("Invalid --attributes JSON: {}", exc)
        fail_with_json(f"Invalid --attributes JSON: {exc}", error_class="UsageError")
    if not isinstance(parsed_attributes, dict):
        fail_with_json("--attributes must be a JSON object", error_class="UsageError")

    management_public_key = Path(management_public_key_file).read_text().strip()
    if not management_public_key:
        fail_with_json("Management public key file is empty", error_class="UsageError")

    workspace_path = Path(workspace_dir)
    if mngr_source is not None:
        _sync_mngr_into_template(Path(mngr_source), workspace_path)

    logger.info("Creating {} pool host(s) with attributes={}", count, parsed_attributes)

    success_count = 0
    failures: list[str] = []
    for i in range(1, count + 1):
        logger.info("[{}/{}]", i, count)
        try:
            is_success = _create_single_pool_host(
                workspace_dir=workspace_path,
                attributes=parsed_attributes,
                management_public_key=management_public_key,
                database_url=database_url,
            )
        except (ConcurrencyGroupError, psycopg2.Error, OSError) as exc:
            logger.warning("[{}] Failed: {}", i, exc)
            failures.append(str(exc))
            is_success = False

        if is_success:
            success_count += 1

    emit_json(
        {
            "requested": count,
            "succeeded": success_count,
            "failed": count - success_count,
            "failures": failures,
        }
    )
    if success_count < count:
        raise SystemExit(1)


@pool.command(name="list")
@click.option(
    "--database-url",
    required=True,
    type=str,
    envvar="DATABASE_URL",
    help="Neon PostgreSQL direct connection string",
)
def pool_list(database_url: str) -> None:
    """List rows in pool_hosts."""
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, vps_ip, agent_id, host_id, status, attributes, "
                "leased_to_user, leased_at, released_at, created_at "
                "FROM pool_hosts ORDER BY created_at DESC"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    emit_json(
        [
            {
                "id": str(row[0]),
                "vps_ip": row[1],
                "agent_id": row[2],
                "host_id": row[3],
                "status": row[4],
                "attributes": row[5],
                "leased_to_user": row[6],
                "leased_at": str(row[7]) if row[7] else None,
                "released_at": str(row[8]) if row[8] else None,
                "created_at": str(row[9]) if row[9] else None,
            }
            for row in rows
        ]
    )


@pool.command(name="destroy")
@click.argument("pool_host_id")
@click.option(
    "--database-url",
    required=True,
    type=str,
    envvar="DATABASE_URL",
    help="Neon PostgreSQL direct connection string",
)
@click.option("--force", is_flag=True, help="Drop the row even if status != 'released'")
def pool_destroy(pool_host_id: str, database_url: str, force: bool) -> None:
    """Remove a pool_hosts row.

    Note: this does NOT destroy the underlying Vultr VPS; that is intentional
    so an operator can use ``mngr destroy`` themselves and inspect the row
    state first.
    """
    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM pool_hosts WHERE id = %s", (pool_host_id,))
                row = cur.fetchone()
                if row is None:
                    fail_with_json(f"No pool_hosts row with id {pool_host_id}", error_class="NotFound")
                if row[0] != "released" and not force:
                    fail_with_json(
                        f"Row {pool_host_id} is in status '{row[0]}'; pass --force to delete anyway",
                        error_class="UnsafeDelete",
                    )
                cur.execute("DELETE FROM pool_hosts WHERE id = %s", (pool_host_id,))
    finally:
        conn.close()
    emit_json({"deleted": True, "pool_host_id": pool_host_id})
