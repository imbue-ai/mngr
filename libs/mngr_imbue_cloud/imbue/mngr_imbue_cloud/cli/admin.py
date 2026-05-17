"""`mngr imbue_cloud admin pool ...` -- operator-only pool provisioning.

Provisions OVH classic VPSes via ``mngr create`` (the imbue-team operator must
have an OVH-configured mngr provider available locally), waits for the agent,
installs + configures ufw on the VPS, installs a management SSH key on both
the VPS and the container, then writes a row to the connector's Neon
``pool_hosts`` table.

Provider-generic by design: extra VPS-side tags (e.g. ``minds_env=<name>``
threaded through by the ``minds pool`` env-aware wrapper) come from
repeatable ``--tag KEY=VALUE`` CLI options. This command itself has no
knowledge of minds environments; that's the caller's responsibility.

Authentication: this command talks to Neon directly via ``DATABASE_URL`` and to
OVH via the operator's local ``mngr`` provider config (or
``OVH_APPLICATION_KEY`` / ``OVH_APPLICATION_SECRET`` / ``OVH_CONSUMER_KEY``
env vars). It does NOT use the operator's SuperTokens session; the connector
is not involved in pool provisioning at all.
"""

import json as _json
import os
import shlex
import sys
import tomllib
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

# Env var name a minds-activated shell uses to flag the pool host DSN
# for the activated env. Mirrors the field name written into
# ``~/.minds-<env>/secrets.toml`` by ``minds env deploy`` so an operator
# can also point us at a one-off DSN by exporting it directly.
_MINDS_HOST_POOL_DSN_ENV_VAR: Final[str] = "MINDS_HOST_POOL_DSN"
# Env vars the minds bootstrap exports on ``minds env activate`` so we
# can locate the per-env secrets.toml without importing any minds
# module (this CLI lives in mngr_imbue_cloud and is intentionally
# decoupled from the minds package).
_MINDS_ROOT_NAME_ENV_VAR: Final[str] = "MINDS_ROOT_NAME"
_MINDS_PREFIX: Final[str] = "minds"


def _read_activated_minds_host_pool_dsn() -> str | None:
    """Return the activated minds env's NEON_HOST_POOL_DSN, or None.

    Walks the same on-disk layout ``minds env deploy`` writes:

        $HOME/.<MINDS_ROOT_NAME>/secrets.toml -> [secrets].NEON_HOST_POOL_DSN

    Returns None when ``MINDS_ROOT_NAME`` is unset, when the env root
    is production (``MINDS_ROOT_NAME=minds``, no per-env secrets.toml),
    when the file doesn't exist, or when the field is missing / empty.
    All of those map to "this CLI has no opinion -- caller must pass
    ``--database-url`` explicitly or set ``MINDS_HOST_POOL_DSN``."
    """
    root_name = os.environ.get(_MINDS_ROOT_NAME_ENV_VAR)
    if not root_name or root_name == _MINDS_PREFIX:
        return None
    secrets_path = Path.home() / f".{root_name}" / "secrets.toml"
    if not secrets_path.is_file():
        return None
    try:
        raw = tomllib.loads(secrets_path.read_text())
    except OSError as exc:
        logger.warning("Could not read {} for pool DSN resolution: {}", secrets_path, exc)
        return None
    except tomllib.TOMLDecodeError as exc:
        logger.warning(
            "Could not parse {} for pool DSN resolution ({}); pass --database-url explicitly.",
            secrets_path,
            exc,
        )
        return None
    secrets_block = raw.get("secrets")
    if not isinstance(secrets_block, dict):
        return None
    dsn = secrets_block.get("NEON_HOST_POOL_DSN")
    if not isinstance(dsn, str) or not dsn:
        return None
    return dsn


def _resolve_pool_database_url(explicit: str | None) -> str:
    """Resolve the pool DSN for an admin pool command.

    Precedence (highest first):

    1. The explicit ``--database-url`` flag, if the operator passed it.
    2. ``$MINDS_HOST_POOL_DSN`` env var.
    3. The activated minds env's ``secrets.toml`` ``NEON_HOST_POOL_DSN``
       field (written by ``minds env deploy`` for dev envs).
    4. Refuse with a useful error.

    Production / staging operators (or anyone running outside an
    activated minds env) keep working: explicit ``--database-url`` is
    still accepted, and ``$DATABASE_URL`` is intentionally NOT consulted
    here -- it's a generic env var that the operator might have pointed
    at a totally unrelated DB. ``MINDS_HOST_POOL_DSN`` is the explicit
    opt-in.
    """
    if explicit:
        return explicit
    env_value = os.environ.get(_MINDS_HOST_POOL_DSN_ENV_VAR)
    if env_value:
        return env_value
    activated_dsn = _read_activated_minds_host_pool_dsn()
    if activated_dsn:
        return activated_dsn
    fail_with_json(
        "No pool DSN available. Either pass --database-url explicitly, export "
        f"{_MINDS_HOST_POOL_DSN_ENV_VAR}=<dsn>, or `minds env activate <dev-env>` "
        "first (deploys write the DSN into the per-env secrets.toml).",
        error_class="UsageError",
    )
    # ``fail_with_json`` raises; this line is unreachable but satisfies
    # the type checker.
    raise AssertionError("unreachable")


_CONTAINER_SSH_PORT: Final[int] = 2222

# 30 min: the inner ``mngr create ... --template ovh`` builds a fresh
# Docker image on the leased VPS, which can take 10-20 min (network bound).
# A previous 10-min cap occasionally killed otherwise-healthy provisions.
_MNGR_COMMAND_TIMEOUT_SECONDS: Final[int] = 1800
_SSH_COMMAND_TIMEOUT_SECONDS: Final[int] = 60

# Manual rsync excludes layered on top of `--filter=:- .gitignore`. The
# filter handles `__pycache__`, `.venv`, `node_modules`, `.test_output`,
# `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `.external_worktrees`, and
# anything else mngr's gitignore lists. These two patterns are NOT in
# .gitignore so we exclude them explicitly:
#   - `.git`: gitignore never lists it; it's git's internal dir.
#   - `uv.lock`: intentionally committed at the mngr root, but each install
#     context should regenerate its own.
_RSYNC_MANUAL_EXCLUDES: Final[tuple[str, ...]] = (".git", "uv.lock")
_GITIGNORE_RSYNC_FILTER: Final[str] = ":- .gitignore"


@click.group(name="admin")
def admin() -> None:
    """Operator-only commands."""


@admin.group(name="pool")
def pool() -> None:
    """Pool host provisioning (OVH + Neon)."""


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
    extra_env: dict[str, str] | None = None,
) -> FinishedProcess:
    """Run a mngr CLI command and return the result.

    When ``is_streaming=True`` the child's stdout and stderr are mirrored
    to our stderr line-by-line via ``_stream_subprocess_line`` (and still
    captured in the returned ``FinishedProcess``). Use this for the
    inner ``mngr create`` during pool baking -- the run takes 8-15
    minutes and otherwise produces no visible output until completion,
    which makes diagnosing pool-bake failures (or just confirming that
    provisioning is making progress) difficult.

    ``extra_env`` merges into ``os.environ`` for the subprocess.
    Used by the pool-bake to thread ``MNGR_VPS_EXTRA_TAGS`` (so the
    spawned VPS gets the activated minds env's ``minds_env=<name>``
    tag) without polluting the calling process's own env.
    """
    full_command = ["mngr"] + args
    logger.info("  Running: {}", " ".join(full_command))
    on_output = _stream_subprocess_line if is_streaming else None
    subprocess_env: dict[str, str] | None = None
    if extra_env:
        subprocess_env = dict(os.environ)
        subprocess_env.update(extra_env)
    cg = ConcurrencyGroup(name="pool-mngr")
    with cg:
        return cg.run_process_to_completion(
            command=full_command,
            timeout=float(timeout),
            is_checked_after=False,
            cwd=cwd,
            on_output=on_output,
            env=subprocess_env,
        )


def _run_ssh_command(
    vps_address: str,
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
        f"root@{vps_address}",
        command,
    ]
    logger.info("  SSH {}:{}: {}", vps_address, port, command)
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


def _get_agent_info(agent_name: str, provider: str = "ovh") -> dict[str, Any] | None:
    """Query mngr list --format json and find the agent by name.

    Scopes to ``--provider <provider>`` (the bake only ever creates on ovh
    today, so the default matches the call site) and passes ``--on-error
    continue`` so unrelated stale hosts on the operator's machine -- e.g. a
    pre-existing leased pool host whose container's ``/code/`` workdir has
    been wiped -- do not abort the listing and lose the just-created agent's
    record. The bake still treats "agent not in output" as a failure: that
    path is handled by the normal "agent_info is None" check at the call
    site, so genuine create failures are not papered over.
    """
    result = _run_mngr_command(
        [
            "list",
            "--format",
            "json",
            "--provider",
            provider,
            "--on-error",
            "continue",
            "--include",
            f'name == "{agent_name}"',
        ],
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
    for pattern in _RSYNC_MANUAL_EXCLUDES:
        exclude_args.extend(["--exclude", pattern])
    command = (
        ["rsync", "-a", "--delete", f"--filter={_GITIGNORE_RSYNC_FILTER}"]
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


def build_extra_tags_env_value(tags: tuple[str, ...]) -> str:
    """Join repeated ``--tag KEY=VALUE`` CLI values into ``MNGR_VPS_EXTRA_TAGS``.

    Each entry must already be a ``KEY=VALUE`` string (validated client-side
    before we ever construct the env var so a typo'd ``--tag foo`` aborts the
    bake with a usage error instead of crashing inside mngr).
    ``mngr_vps_docker.build_vps_tags`` and ``mngr_ovh.iam_tags.parse_extra_tags_env``
    both consume the comma-separated form.
    """
    for entry in tags:
        if "=" not in entry:
            raise click.UsageError(f"--tag value must be KEY=VALUE, got: {entry!r}")
    return ",".join(tags)


def _ufw_provision_commands(container_ssh_port: int) -> tuple[str, ...]:
    """Return the sequence of root-shell commands that install + configure ufw.

    The order matters: allow port 22 *before* enabling ufw, otherwise enabling
    severs the in-progress SSH session that runs the next command. Default
    policy is deny-incoming + allow-outgoing once 22 and the container sshd
    port are explicitly allowed.
    """
    return (
        "apt-get update",
        "DEBIAN_FRONTEND=noninteractive apt-get install -y ufw",
        "ufw allow 22/tcp",
        f"ufw allow {container_ssh_port}/tcp",
        "ufw default allow outgoing",
        "ufw default deny incoming",
        "ufw --force enable",
    )


def _checked_ssh_command(vps_address: str, ssh_key_path: str, port: int, command: str, *, label: str) -> None:
    """Run an SSH command and raise on non-zero exit.

    Used for steps that MUST succeed (ufw install/configure, management key
    install). The bake aborts on failure rather than continuing with a
    half-configured host that would silently land in the pool.
    """
    if not _run_ssh_command(vps_address, ssh_key_path, port, command):
        raise PoolBakeError(f"{label} failed on VPS {vps_address}; aborting bake")


class PoolBakeError(RuntimeError):
    """Raised when a required pool-bake step fails irrecoverably."""


def _create_single_pool_host(
    workspace_dir: Path,
    attributes: dict[str, Any],
    management_public_key: str,
    database_url: str,
    region: str,
    extra_tags: tuple[str, ...],
) -> bool:
    """Create a single pool host. Returns True on success.

    Inserts a row with the request-side ``attributes`` dict so the connector's
    ``attributes @>`` match can find it. ``extra_tags`` is a tuple of
    ``KEY=VALUE`` strings forwarded as ``MNGR_VPS_EXTRA_TAGS`` to the inner
    ``mngr create``; ``mngr_ovh`` then attaches them as additional OVH IAM
    v2 tags alongside ``mngr-provider`` / ``mngr-host-id``.
    """
    suffix = uuid4().hex
    agent_name = f"pool-{suffix}"
    host_name = f"{agent_name}-host"
    address = f"{agent_name}@{host_name}.ovh"

    logger.info("Creating pool host: {} (region={})", address, region)

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
        "ovh",
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
        # Per-bake region: the ``ovh`` create template does NOT bake one
        # in, so every host can land in a different OVH datacenter.
        "-b",
        f"--vps-datacenter={region}",
    ]

    pool_create_env: dict[str, str] | None = None
    if extra_tags:
        pool_create_env = {"MNGR_VPS_EXTRA_TAGS": build_extra_tags_env_value(extra_tags)}
        logger.info("  Tagging VPS with extra tags: {}", pool_create_env["MNGR_VPS_EXTRA_TAGS"])

    create_result = _run_mngr_command(mngr_command, cwd=workspace_dir, is_streaming=True, extra_env=pool_create_env)
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

    vps_address = ssh.get("host")
    if not isinstance(vps_address, str):
        logger.error("No VPS address in SSH info")
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

    # Install + configure ufw on the VPS. Each step must succeed; we bail
    # on the whole bake if anything fails (otherwise the host would land
    # in the pool with no firewall and a half-applied policy).
    logger.info("  Installing + configuring ufw on VPS {}", vps_address)
    for ufw_command in _ufw_provision_commands(_CONTAINER_SSH_PORT):
        _checked_ssh_command(vps_address, vps_key_path, 22, ufw_command, label=f"ufw step {ufw_command!r}")

    key_line = shlex.quote(management_public_key.strip())
    install_cmd = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo "
        + key_line
        + " >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    )
    _checked_ssh_command(vps_address, vps_key_path, 22, install_cmd, label="install management key on VPS")

    logger.info("  Installing management key in container via mngr exec")
    container_install = _run_mngr_command(["exec", agent_name, install_cmd], timeout=60)
    if container_install.returncode != 0:
        raise PoolBakeError(
            f"installing management key inside container for {agent_name} failed: {container_install.stderr.strip()}"
        )

    row_id = uuid4()
    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pool_hosts "
                    "(id, vps_address, vps_instance_id, agent_id, host_id, ssh_port, ssh_user, "
                    "container_ssh_port, status, attributes, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, 22, 'root', %s, 'available', %s::jsonb, NOW())",
                    (
                        str(row_id),
                        vps_address,
                        host_id,
                        agent_id,
                        host_id,
                        _CONTAINER_SSH_PORT,
                        _json.dumps(attributes),
                    ),
                )
    finally:
        conn.close()

    logger.info("  Pool host ready: id={}, agent_id={}, vps_address={}", row_id, agent_id, vps_address)
    return True


@pool.command(name="create")
@click.option("--count", required=True, type=int, help="Number of pool hosts to create")
@click.option(
    "--region",
    required=True,
    type=str,
    help=(
        "OVH datacenter code for the new pool VPSes (e.g. ``US-EAST-VA``, ``US-WEST-OR``). "
        "Validated by OVH at order time; failure surfaces as a 'datacenter not allowed for this plan' error."
    ),
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help=(
        "Repeatable ``KEY=VALUE`` tag attached to every freshly-provisioned VPS via the OVH IAM v2 "
        "tag system. Forwarded to the inner ``mngr create`` as ``MNGR_VPS_EXTRA_TAGS=k1=v1,k2=v2``. "
        "Example: ``--tag minds_env=alice --tag pool-owner=bob``."
    ),
)
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
    required=False,
    default=None,
    type=str,
    help=(
        "Neon PostgreSQL direct connection string for the pool DB. Defaults to "
        "MINDS_HOST_POOL_DSN env var, or the activated minds env's "
        "secrets.toml NEON_HOST_POOL_DSN field (so `minds env activate <dev-env>` "
        "is enough). Pass this explicitly when operating outside an activated env."
    ),
)
@click.option(
    "--mngr-source",
    type=click.Path(exists=True),
    default=None,
    help="Path to the mngr monorepo root. If provided, rsyncs into the template's vendor/mngr/ before creating hosts.",
)
def pool_create(
    count: int,
    region: str,
    tags: tuple[str, ...],
    attributes_json: str,
    workspace_dir: str,
    management_public_key_file: str,
    database_url: str | None,
    mngr_source: str | None,
) -> None:
    """Create pre-provisioned pool hosts."""
    resolved_database_url = _resolve_pool_database_url(database_url)
    try:
        parsed_attributes = _json.loads(attributes_json)
    except _json.JSONDecodeError as exc:
        logger.error("Invalid --attributes JSON: {}", exc)
        fail_with_json(f"Invalid --attributes JSON: {exc}", error_class="UsageError")
    if not isinstance(parsed_attributes, dict):
        fail_with_json("--attributes must be a JSON object", error_class="UsageError")
    # Validate ``--tag`` shapes up front so we don't bake the first
    # host and then trip over a typo on the second one.
    try:
        build_extra_tags_env_value(tags)
    except click.UsageError as exc:
        fail_with_json(str(exc), error_class="UsageError")

    management_public_key = Path(management_public_key_file).read_text().strip()
    if not management_public_key:
        fail_with_json("Management public key file is empty", error_class="UsageError")

    workspace_path = Path(workspace_dir)
    if mngr_source is not None:
        _sync_mngr_into_template(Path(mngr_source), workspace_path)

    logger.info(
        "Creating {} pool host(s) with region={}, attributes={}, tags={}",
        count,
        region,
        parsed_attributes,
        list(tags),
    )

    success_count = 0
    failures: list[str] = []
    for i in range(1, count + 1):
        logger.info("[{}/{}]", i, count)
        try:
            is_success = _create_single_pool_host(
                workspace_dir=workspace_path,
                attributes=parsed_attributes,
                management_public_key=management_public_key,
                database_url=resolved_database_url,
                region=region,
                extra_tags=tags,
            )
        except (ConcurrencyGroupError, PoolBakeError, psycopg2.Error, OSError) as exc:
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
    required=False,
    default=None,
    type=str,
    help=(
        "Neon PostgreSQL direct connection string for the pool DB. Defaults to "
        "MINDS_HOST_POOL_DSN env var, or the activated minds env's "
        "secrets.toml NEON_HOST_POOL_DSN field (so `minds env activate <dev-env>` "
        "is enough). Pass this explicitly when operating outside an activated env."
    ),
)
def pool_list(database_url: str | None) -> None:
    """List rows in pool_hosts."""
    resolved_database_url = _resolve_pool_database_url(database_url)
    conn = psycopg2.connect(resolved_database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, vps_address, agent_id, host_id, status, attributes, "
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
                "vps_address": row[1],
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
    required=False,
    default=None,
    type=str,
    help=(
        "Neon PostgreSQL direct connection string for the pool DB. Defaults to "
        "MINDS_HOST_POOL_DSN env var, or the activated minds env's "
        "secrets.toml NEON_HOST_POOL_DSN field (so `minds env activate <dev-env>` "
        "is enough). Pass this explicitly when operating outside an activated env."
    ),
)
@click.option("--force", is_flag=True, help="Drop the row even if status != 'released'")
def pool_destroy(pool_host_id: str, database_url: str | None, force: bool) -> None:
    """Remove a pool_hosts row.

    Note: this does NOT destroy the underlying OVH VPS; that is intentional
    so an operator can use ``mngr destroy`` themselves and inspect the row
    state first.
    """
    resolved_database_url = _resolve_pool_database_url(database_url)
    conn = psycopg2.connect(resolved_database_url)
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
