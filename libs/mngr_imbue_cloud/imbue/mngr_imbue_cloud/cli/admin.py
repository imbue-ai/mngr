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
from imbue.mngr_ovh.client import build_ovh_client
from imbue.mngr_ovh.config import OvhProviderConfig
from imbue.mngr_ovh.iam_tags import MNGR_PROVIDER_TAG_KEY
from imbue.mngr_ovh.iam_tags import delete_tag
from imbue.mngr_ovh.iam_tags import get_vps_resource
from imbue.mngr_ovh.iam_tags import iam_region_code_for_endpoint
from imbue.mngr_ovh.iam_tags import vps_urn_for
from imbue.mngr_vps_docker.primitives import VpsInstanceId

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

# Constant agent name baked onto every pool host. The minds-side
# adoption code (``ImbueCloudHost.create_agent_state``) explicitly keeps
# the bake's agent name verbatim -- so the bake must use the same name
# the user's ``mngr create system-services@<host>.imbue_cloud_<slug>``
# does, otherwise the user's lease ends up with an agent whose tmux
# session is named after a per-bake UUID instead of ``system-services``.
# Tracked at ``libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/host.py:67-75``
# (the ``ImbueCloudHost`` docstring spells the contract out).
_BAKED_SERVICES_AGENT_NAME: Final[str] = "system-services"

# Path inside the pool host's container of the FCT bootstrap's
# initial-chat sentinel. The bootstrap writes this file after creating
# the chat agent on first boot, then skips the create on every later
# boot if it sees the file. Removing it at bake time means the user's
# first lease + start re-fires the create, this time with the
# lease-rewritten ``/mngr/data.json`` ``host_name`` (the user's
# workspace name) so the chat agent inherits the right name. The file
# lives inside ``runtime/`` which is already a git worktree at bake
# time, so ``_init_runtime_worktree`` skips on every subsequent
# bootstrap run and the in-container ``rm`` is enough (no commit or
# push needed -- the on-disk working-tree state survives all subsequent
# mngr stop/start cycles inside the same container).
_INITIAL_CHAT_SENTINEL_PATH: Final[str] = "/code/runtime/initial_chat_created"

# mngr env-override key that turns off the OVH provider's cancelled-VPS recycling
# for the inner ``mngr create``. Setting it forces a fresh OVH order instead of
# reclaiming a cancelled VPS -- useful for testing the fresh-provision path.
_OVH_ENABLE_RECYCLE_ENV_KEY: Final[str] = "MNGR__PROVIDERS__OVH__ENABLE_RECYCLE_CANCELLED"

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

# INSERT statement for a freshly-baked pool host row. The column list MUST
# stay in sync with the ``pool_hosts`` schema declared in
# ``apps/remote_service_connector/migrations/*.sql``: any NOT NULL column
# without a DB-side default has to appear here, otherwise the bake
# succeeds in the cloud (VPS provisioned, image built, key injected)
# but the final DB write 500s with a NOT NULL violation -- leaving a
# stranded VPS with no DB row. ``host_name`` was the first such drift
# (added to the schema; missed in the INSERT until 2026-05). Tested in
# ``admin_test.py::test_pool_hosts_insert_has_required_columns``.
_INSERT_POOL_HOST_SQL: Final[str] = (
    "INSERT INTO pool_hosts "
    "(id, vps_address, vps_instance_id, agent_id, host_id, host_name, ssh_port, ssh_user, "
    "container_ssh_port, status, attributes, region, created_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, 22, 'root', %s, 'available', %s::jsonb, %s, NOW())"
)


def build_pool_host_insert_values(
    *,
    row_id: str,
    vps_address: str,
    agent_id: str,
    host_id: str,
    host_name: str,
    container_ssh_port: int,
    attributes_json: str,
    # OVH datacenter code the VPS was ordered in; persisted so the connector can
    # apply region-aware lease filtering/ordering.
    region: str,
) -> tuple[str, str, str, str, str, str, int, str, str]:
    """Build the value tuple for :data:`_INSERT_POOL_HOST_SQL`.

    ``vps_instance_id`` MUST be the OVH service name -- it is what every
    connector-side OVH teardown call keys on (``vps_urn_for`` and
    ``set_delete_at_expiration`` in the connector's ``clean_up_pool_host_in_ovh``,
    the release route, and the hourly cleanup sweep). For these OVH-backed pool
    hosts the service name is the ``vps_address`` (the ``vps-xxxx.vps.ovh.us``
    hostname). An earlier version wrote the mngr ``host_id`` (a ``host-...`` id)
    here, which made every OVH cancellation silently 404 -- VPSes were never
    cancelled and kept billing. Kept as a pure function so the column-to-value
    mapping is pinned by a unit test without standing up a real bake.
    """
    return (
        row_id,
        vps_address,
        # vps_instance_id: the OVH service name, NOT host_id (see docstring).
        vps_address,
        agent_id,
        host_id,
        host_name,
        container_ssh_port,
        attributes_json,
        region,
    )


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


def _get_agent_info(
    agent_name: str,
    *,
    host_name: str,
    provider: str = "ovh",
) -> dict[str, Any] | None:
    """Query mngr list --format json and find the agent by name + host name.

    Scopes to ``--provider <provider>`` (the bake only ever creates on ovh
    today, so the default matches the call site) and passes ``--on-error
    continue`` so unrelated stale hosts on the operator's machine -- e.g. a
    pre-existing leased pool host whose container's ``/code/`` workdir has
    been wiped -- do not abort the listing and lose the just-created agent's
    record. The bake still treats "agent not in output" as a failure: that
    path is handled by the normal "agent_info is None" check at the call
    site, so genuine create failures are not papered over.

    ``host_name`` MUST be the bake's per-bake-unique host name (not the
    constant agent name). The operator's local mngr state accumulates one
    ``system-services`` agent per bake (each on a different host), so
    filtering on agent name alone returns the first match -- which under
    sequential bakes is some prior bake's stale agent on a different VPS.
    Disambiguating on ``host.name`` makes the lookup unambiguous because
    the bake's host name carries a per-bake hex suffix.
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
            f'name == "{agent_name}" && host.name == "{host_name}"',
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
        if not isinstance(agent, dict) or agent.get("name") != agent_name:
            continue
        host = agent.get("host")
        if isinstance(host, dict) and host.get("name") == host_name:
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
        raise PoolBakeError(
            f"rsync of {mngr_source} into {vendor_mngr} failed (exit {result.returncode}): {result.stderr.strip()}"
        )


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
    is_recycle_enabled: bool,
) -> bool:
    """Create a single pool host. Returns True on success.

    Inserts a row with the request-side ``attributes`` dict so the connector's
    ``attributes @>`` match can find it. ``extra_tags`` is a tuple of
    ``KEY=VALUE`` strings forwarded as ``MNGR_VPS_EXTRA_TAGS`` to the inner
    ``mngr create``; ``mngr_ovh`` then attaches them as additional OVH IAM
    v2 tags alongside ``mngr-provider`` / ``mngr-host-id``.

    When ``is_recycle_enabled`` is False, the inner ``mngr create`` gets the
    ``MNGR__PROVIDERS__OVH__ENABLE_RECYCLE_CANCELLED=false`` env override so the
    OVH provider orders a fresh VPS instead of reclaiming a cancelled one.
    """
    suffix = uuid4().hex
    # ``agent_name`` is the contract with ``ImbueCloudHost.create_agent_state``
    # (see ``host.py`` docstring): adoption preserves the bake's name
    # verbatim, so this must match the minds-side default agent name
    # ``system-services`` to give the user's tmux session a sane label
    # instead of a per-bake UUID. ``host_name`` keeps the suffix so the
    # operator's local mngr state can distinguish pool hosts across
    # sequential bakes; the user's chosen workspace name overwrites it at
    # lease time via ``ImbueCloudProvider.create_host``.
    agent_name = _BAKED_SERVICES_AGENT_NAME
    host_name = f"pool-{suffix}-host"
    address = f"{agent_name}@{host_name}.ovh"
    # The FCT bootstrap's ``_create_initial_chat_agent`` derives the
    # chat-agent name from ``$MNGR_HOST_DIR/data.json``'s ``host_name``
    # (see ``forever-claude-template/libs/bootstrap/src/bootstrap/manager.py``
    # :func:`_build_create_chat_command`). Captured here so the post-bake
    # cleanup can target it by name for ``mngr destroy``.
    bootstrap_chat_agent_name = host_name

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
        f"--ovh-datacenter={region}",
    ]

    pool_create_env: dict[str, str] = {}
    if extra_tags:
        pool_create_env["MNGR_VPS_EXTRA_TAGS"] = build_extra_tags_env_value(extra_tags)
        logger.info("  Tagging VPS with extra tags: {}", pool_create_env["MNGR_VPS_EXTRA_TAGS"])
    if not is_recycle_enabled:
        pool_create_env[_OVH_ENABLE_RECYCLE_ENV_KEY] = "false"
        logger.info("  Recycling disabled: forcing a fresh OVH VPS order (no cancelled-VPS reuse)")

    create_result = _run_mngr_command(
        mngr_command, cwd=workspace_dir, is_streaming=True, extra_env=pool_create_env or None
    )
    if create_result.returncode != 0:
        logger.error("mngr create failed: {}", create_result.stderr)
        return False

    logger.info("  Created agent: {}", agent_name)

    # The agent NAME (``system-services``) is constant across every bake,
    # so the operator's local mngr state can accumulate several
    # ``system-services`` agents (one per bake, each on a different
    # host). Subsequent ``mngr stop``/``mngr exec`` calls in this bake
    # MUST use the per-bake-unique address to target the just-created
    # agent specifically -- ``mngr stop system-services`` alone is
    # ambiguous under sequential bakes and can pick the wrong one
    # (silently, since ``--on-error continue`` is the mngr default).
    full_address = f"{agent_name}@{host_name}.ovh"

    stop_result = _run_mngr_command(["stop", full_address])
    if stop_result.returncode != 0:
        raise PoolBakeError(
            f"`mngr stop {full_address}` failed (exit {stop_result.returncode}): {stop_result.stderr.strip()}"
        )

    logger.info("  Ensuring sshd is running in container")
    # Match the cloud-init bump we apply to the host VPS (and the lima
    # provider's sshd config): the default ``MaxStartups=10:30:100``
    # caps the pre-auth queue tightly, and the imbue_cloud lease + claim
    # flow plus parallel ``mngr observe`` discovery routinely exceeds it
    # and loses connections mid-rsync. ``shlex.join`` packs the whole
    # sshd invocation into a single ``mngr exec`` COMMAND positional
    # (mngr exec parses ``AGENTS... COMMAND`` so multi-token commands
    # have to arrive as one shell-quoted string).
    sshd_command = shlex.join(["/usr/sbin/sshd", "-o", "MaxSessions=100", "-o", "MaxStartups=100:30:200"])
    _run_mngr_command(
        ["exec", full_address, sshd_command],
        timeout=30,
    )

    agent_info = _get_agent_info(agent_name, host_name=host_name)
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
    container_install = _run_mngr_command(["exec", full_address, install_cmd], timeout=60)
    if container_install.returncode != 0:
        raise PoolBakeError(
            f"installing management key inside container for {agent_name} failed: {container_install.stderr.strip()}"
        )

    # During the bake the services agent booted and the FCT bootstrap created
    # an initial chat agent named after the BAKE's host name. That name is
    # wrong for the user's eventual workspace (the user picks their own host
    # name at lease time), and the bootstrap won't recreate the chat agent
    # on later starts because of the sentinel file it wrote. Tear both
    # down so the user's first start gets a fresh chat agent named after
    # their own host name.
    logger.info("  Destroying bootstrap-created chat agent: {}", bootstrap_chat_agent_name)
    # ``mngr exec`` parses ``AGENTS... COMMAND`` -- the LAST positional
    # goes to ``COMMAND`` and the rest to ``AGENTS``. Passing a multi-
    # token command as separate argv entries makes click treat all but
    # the last as agent names ("No agent(s) found matching: destroy,
    # mngr, pool-..."). Pack the whole remote command into one
    # shell-quoted string so it lands as a single ``COMMAND`` positional;
    # the remote sshd parses it back via bash.
    chat_destroy_cmd = shlex.join(["mngr", "destroy", bootstrap_chat_agent_name, "--force"])
    chat_destroy = _run_mngr_command(
        ["exec", full_address, chat_destroy_cmd],
        timeout=120,
    )
    if chat_destroy.returncode != 0:
        # Failing the bake instead of warning -- a destroy that errors
        # out almost always means a vendored-mngr / FCT-template version
        # skew (e.g. an `agent_types` field the older vendored mngr
        # doesn't recognize), and shipping a pool host whose internal
        # bootstrap state we don't actually understand has bitten us
        # before. Better to abort + clean up than land a half-known
        # host in the pool.
        raise PoolBakeError(
            f"destroying bootstrap chat agent {bootstrap_chat_agent_name!r} via "
            f"`mngr exec {full_address} {chat_destroy_cmd!r}` failed "
            f"(exit {chat_destroy.returncode}): {chat_destroy.stderr.strip()}"
        )

    logger.info("  Removing initial-chat sentinel: {}", _INITIAL_CHAT_SENTINEL_PATH)
    sentinel_rm_cmd = shlex.join(["rm", "-f", _INITIAL_CHAT_SENTINEL_PATH])
    sentinel_rm = _run_mngr_command(
        ["exec", full_address, sentinel_rm_cmd],
        timeout=30,
    )
    if sentinel_rm.returncode != 0:
        raise PoolBakeError(
            f"removing initial-chat sentinel {_INITIAL_CHAT_SENTINEL_PATH!r} failed: {sentinel_rm.stderr.strip()}"
        )

    row_id = uuid4()
    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    _INSERT_POOL_HOST_SQL,
                    build_pool_host_insert_values(
                        row_id=str(row_id),
                        vps_address=vps_address,
                        agent_id=agent_id,
                        host_id=host_id,
                        host_name=host_name,
                        container_ssh_port=_CONTAINER_SSH_PORT,
                        attributes_json=_json.dumps(attributes),
                        region=region,
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
@click.option(
    "--no-recycle",
    "is_recycle_enabled",
    flag_value=False,
    default=True,
    help=(
        "Force a fresh OVH VPS order instead of reclaiming a cancelled VPS. By default the OVH "
        "provider recycles a cancelled (still-billable) VPS when one is available; pass this to "
        "test the fresh-provision path. Sets MNGR__PROVIDERS__OVH__ENABLE_RECYCLE_CANCELLED=false "
        "on the inner `mngr create`."
    ),
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
    is_recycle_enabled: bool,
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
                is_recycle_enabled=is_recycle_enabled,
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


def _cancel_pool_host_vps(service_name: str) -> None:
    """Strip per-lease OVH tags and cancel the VPS, leaving it blank + recyclable.

    Mirrors the connector's release teardown so ``pool destroy`` can never strand
    a still-billing VPS: strip every IAM tag except ``mngr-provider`` (so the next
    bake can recycle the cancelled host), then set ``deleteAtExpiration=True`` via
    ``OvhVpsClient.destroy_instance``. Reuses ``mngr_ovh`` (reachable through the
    vps_docker dependency) with OVH AK/AS/CK read from the environment -- the minds
    ``pool destroy`` wrapper injects them from Vault. Raises on any OVH failure, so
    the caller does NOT delete the DB row while the VPS is still running.
    """
    client = build_ovh_client(OvhProviderConfig())
    region = iam_region_code_for_endpoint(os.environ.get("OVH_ENDPOINT", "ovh-us"))
    urn = vps_urn_for(service_name, region_code=region)
    resource = get_vps_resource(client, urn)
    if resource is not None:
        for key in resource.tags:
            if key != MNGR_PROVIDER_TAG_KEY:
                delete_tag(client, urn, key)
    client.destroy_instance(VpsInstanceId(service_name))


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
@click.option(
    "--skip-vps-cancel",
    is_flag=True,
    default=False,
    help=(
        "Only drop the DB row; do NOT cancel the OVH VPS. Use exclusively when the "
        "VPS is already gone/cancelled -- otherwise the default path cancels it so "
        "no billing orphan is left behind."
    ),
)
def pool_destroy(pool_host_id: str, database_url: str | None, force: bool, skip_vps_cancel: bool) -> None:
    """Remove a pool_hosts row, cancelling its OVH VPS first (full teardown).

    By default this cancels the underlying OVH VPS (strip per-lease tags +
    ``deleteAtExpiration=True``) *before* deleting the row, so it leaves the host
    in the same blank/cancelled/recyclable state a proper release does -- never a
    stranded, still-billing VPS. Pass ``--skip-vps-cancel`` only when the VPS is
    already gone. Cancellation needs OVH credentials in the environment (the minds
    ``pool destroy`` wrapper injects them from Vault).
    """
    resolved_database_url = _resolve_pool_database_url(database_url)
    conn = psycopg2.connect(resolved_database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, vps_address FROM pool_hosts WHERE id = %s", (pool_host_id,))
            row = cur.fetchone()
            if row is None:
                fail_with_json(f"No pool_hosts row with id {pool_host_id}", error_class="NotFound")
            status, vps_address = row
            if status != "released" and not force:
                fail_with_json(
                    f"Row {pool_host_id} is in status '{status}'; pass --force to delete anyway",
                    error_class="UnsafeDelete",
                )
        # Cancel the VPS BEFORE deleting the row: if the cancel fails we keep the
        # row so the teardown stays retryable (no silent orphan).
        if not skip_vps_cancel:
            if not vps_address:
                fail_with_json(
                    f"Row {pool_host_id} has no vps_address; cannot cancel its VPS. "
                    "Pass --skip-vps-cancel if the VPS is already gone.",
                    error_class="UnsafeDelete",
                )
            _cancel_pool_host_vps(vps_address)
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pool_hosts WHERE id = %s", (pool_host_id,))
    finally:
        conn.close()
    emit_json({"deleted": True, "pool_host_id": pool_host_id, "vps_cancelled": not skip_vps_cancel})
