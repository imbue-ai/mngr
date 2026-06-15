"""`mngr imbue_cloud admin pool ...` -- operator-only pool provisioning.

``pool create`` bakes pre-provisioned pool hosts on a chosen ``--backend``: an OVH
classic VPS ordered on demand (``ovh_vps``, the default) or a lima-VM "slice" carved
on one of our registered bare-metal boxes (``slice``; the shared implementation is
``cli.server.allocate_slices``). Both bake the same FCT pool host and write the same
kind of leasable row to the connector's Neon ``pool_hosts`` table -- only the
machine-provisioning step differs (order-a-VPS vs. carve-a-VM). The OVH path also
installs + configures ufw and a management SSH key on the VPS + container.

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
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

import click
import psycopg2
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.mngr_imbue_cloud.bake.pool_bake import BAKED_SERVICES_AGENT_NAME
from imbue.mngr_imbue_cloud.bake.pool_bake import BakedPoolHost
from imbue.mngr_imbue_cloud.bake.pool_bake import PoolBakeError
from imbue.mngr_imbue_cloud.bake.pool_bake import bake_pool_host
from imbue.mngr_imbue_cloud.bake.pool_bake import finalize_baked_pool_host
from imbue.mngr_imbue_cloud.bake.pool_bake import run_mngr_command
from imbue.mngr_imbue_cloud.bake.pool_bake import sync_mngr_into_template
from imbue.mngr_imbue_cloud.bake.pool_bake import wait_for_deferred_install
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json
from imbue.mngr_imbue_cloud.cli._common import resolve_pool_database_url
from imbue.mngr_imbue_cloud.cli.server import allocate_slices
from imbue.mngr_ovh.client import build_ovh_client
from imbue.mngr_ovh.config import OvhProviderConfig
from imbue.mngr_ovh.iam_tags import MNGR_PROVIDER_TAG_KEY
from imbue.mngr_ovh.iam_tags import delete_tag
from imbue.mngr_ovh.iam_tags import get_vps_resource
from imbue.mngr_ovh.iam_tags import iam_region_code_for_endpoint
from imbue.mngr_ovh.iam_tags import vps_urn_for
from imbue.mngr_vps_docker.primitives import VpsInstanceId

_CONTAINER_SSH_PORT: Final[int] = 2222

# mngr env-override key that turns off the OVH provider's cancelled-VPS recycling
# for the inner ``mngr create``. Setting it forces a fresh OVH order instead of
# reclaiming a cancelled VPS -- useful for testing the fresh-provision path.
_OVH_ENABLE_RECYCLE_ENV_KEY: Final[str] = "MNGR__PROVIDERS__OVH__ENABLE_RECYCLE_CANCELLED"

_SSH_COMMAND_TIMEOUT_SECONDS: Final[int] = 60

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


def _harden_ovh_vps(management_public_key: str, baked: BakedPoolHost, full_address: str) -> None:
    """OVH-specific ``on_host_ready`` step run mid-bake (ufw + management key).

    These steps are OVH-only: a freshly-ordered OVH VPS needs ufw installed +
    configured and the pool management key authorized on both the VPS and the
    container (slices instead authorize the pool key at carve time and rely on the
    box's own firewall + lima's port-forwarding, so they pass no hook). The VPS SSH
    endpoint + on-disk key come from ``mngr create --format json`` (the
    ``vps_ssh_key`` sits next to the container key the bake recorded). Called by
    the OVH bake right after the shared FCT bake returns.
    """
    if not baked.ssh_host or not baked.ssh_key_path:
        raise PoolBakeError("`mngr create --format json` did not expose the VPS ssh endpoint for hardening")
    vps_address = baked.ssh_host
    vps_key_path = str(Path(baked.ssh_key_path).parent / "vps_ssh_key")
    # Install + configure ufw on the VPS. Each step must succeed; we bail on the
    # whole bake if anything fails (otherwise the host would land in the pool with
    # no firewall and a half-applied policy).
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
    container_install = run_mngr_command(["exec", full_address, install_cmd], timeout=60)
    if container_install.returncode != 0:
        raise PoolBakeError(
            f"installing management key inside container for {full_address} failed: {container_install.stderr.strip()}"
        )


def _ovh_run_in_container(
    baked: BakedPoolHost, label: str, command: str, timeout_seconds: float
) -> tuple[int | None, str, str]:
    """Run a shell command inside an OVH pool host's container via ``mngr exec`` (login shell).

    The :class:`~imbue.mngr_imbue_cloud.bake.pool_bake.ContainerCommandRunner` for OVH:
    the agent is resolvable in the operator's mngr state, so ``mngr exec`` reaches
    the container; wrapping in ``bash -lc`` puts ``uv``/``mngr`` on PATH in the FCT
    image. Returns ``(returncode, stdout, stderr)``.
    """
    address = f"{BAKED_SERVICES_AGENT_NAME}@{baked.host_name}.ovh"
    wrapped = f"bash -lc {shlex.quote(command)}"
    result = run_mngr_command(["exec", address, wrapped], timeout=int(timeout_seconds))
    return result.returncode, result.stdout, result.stderr


def _create_single_pool_host(
    workspace_dir: Path,
    attributes: dict[str, Any],
    management_public_key: str,
    database_url: str,
    region: str,
    extra_tags: tuple[str, ...],
    is_recycle_enabled: bool,
) -> bool:
    """Create a single OVH pool host. Returns True on success.

    Delegates the provider-generic FCT create + parse to :func:`bake_pool_host` and
    the shared container sshd-harden + chat-agent teardown to
    :func:`finalize_baked_pool_host` (via the ``mngr exec`` transport), supplying
    only the OVH-specific pieces: the ``ovh`` provider + per-bake datacenter, the
    cancelled-VPS recycle override, stopping the services agent, the ufw +
    management-key install, the extra OVH IAM tags, and the OVH ``pool_hosts``
    insert. The row's ``attributes`` are the request-side dict so the connector's
    ``attributes @>`` match can find it.

    ``extra_tags`` is a tuple of ``KEY=VALUE`` strings forwarded as
    ``MNGR_VPS_EXTRA_TAGS`` to the inner ``mngr create``; ``mngr_ovh`` attaches
    them as additional OVH IAM v2 tags. When ``is_recycle_enabled`` is False the
    OVH provider orders a fresh VPS instead of reclaiming a cancelled one.
    """
    host_name = f"pool-{uuid4().hex}-host"
    logger.info("Creating OVH pool host: {} (region={})", host_name, region)

    # Per-bake region: the ``ovh`` create template does NOT bake one in, so every
    # host can land in a different OVH datacenter. ``--pass-host-env MNGR_PREFIX``
    # keeps the VPS's mngr on the operator's prefix.
    extra_create_args = ["--pass-host-env", "MNGR_PREFIX", "-b", f"--ovh-datacenter={region}"]

    extra_create_env: dict[str, str] = {}
    if extra_tags:
        extra_create_env["MNGR_VPS_EXTRA_TAGS"] = build_extra_tags_env_value(extra_tags)
        logger.info("  Tagging VPS with extra tags: {}", extra_create_env["MNGR_VPS_EXTRA_TAGS"])
    if not is_recycle_enabled:
        extra_create_env[_OVH_ENABLE_RECYCLE_ENV_KEY] = "false"
        logger.info("  Recycling disabled: forcing a fresh OVH VPS order (no cancelled-VPS reuse)")

    baked = bake_pool_host(
        provider_instance="ovh",
        host_name=host_name,
        attributes=attributes,
        workspace_dir=workspace_dir,
        extra_create_args=extra_create_args,
        extra_create_env=extra_create_env or None,
    )
    if not baked.ssh_host:
        raise PoolBakeError(f"baked OVH host {host_name} has no ssh_host; cannot insert pool row")

    full_address = f"{BAKED_SERVICES_AGENT_NAME}@{host_name}.ovh"
    # Let the FCT deferred-install (heavy apt + browser download, kicked off at boot)
    # finish before we stop the services agent: stopping mid-apt corrupts dpkg.
    wait_for_deferred_install(_ovh_run_in_container, baked, host_name=host_name)
    # Stop the freshly-baked services agent (it boots during create); the user's
    # lease re-starts it, which re-runs the FCT bootstrap and re-creates the chat
    # agent under the lease's workspace name. (Slices do the equivalent stop inside
    # the container in ``cli.server._bake_one_slice`` -- keep the two in sync.) Use
    # the per-bake-unique address so sequential bakes don't stop the wrong
    # `system-services` agent.
    stop_result = run_mngr_command(["stop", full_address], timeout=120)
    if stop_result.returncode != 0:
        raise PoolBakeError(
            f"`mngr stop {full_address}` failed (exit {stop_result.returncode}): {stop_result.stderr.strip()}"
        )
    # OVH-specific host hardening: a fresh OVH VPS needs ufw + the pool management
    # key (slices authorize the pool key at carve time, so they skip this). The
    # host is not yet in the pool (no row), so the brief pre-ufw window is not
    # user-reachable.
    _harden_ovh_vps(management_public_key, baked, full_address)
    # Shared FCT post-bake: harden the container sshd + tear down the bootstrap
    # chat agent, over the OVH (mngr exec) transport.
    finalize_baked_pool_host(_ovh_run_in_container, baked, host_name=host_name)

    row_id = uuid4()
    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    _INSERT_POOL_HOST_SQL,
                    build_pool_host_insert_values(
                        row_id=str(row_id),
                        vps_address=baked.ssh_host,
                        agent_id=baked.agent_id,
                        host_id=baked.host_id,
                        host_name=host_name,
                        container_ssh_port=_CONTAINER_SSH_PORT,
                        attributes_json=_json.dumps(attributes),
                        region=region,
                    ),
                )
    finally:
        conn.close()

    logger.info("  Pool host ready: id={}, agent_id={}, vps_address={}", row_id, baked.agent_id, baked.ssh_host)
    return True


@pool.command(name="create")
@click.option("--count", required=True, type=int, help="Number of pool hosts to create")
@click.option(
    "--backend",
    type=click.Choice(["ovh_vps", "slice"]),
    default="ovh_vps",
    show_default=True,
    help=(
        "Which machine backs each pool host. ``ovh_vps`` orders an OVH classic VPS on demand; "
        "``slice`` carves a lima VM on one of our registered bare-metal boxes (run `admin server "
        "register` + `prep` first). Both bake the same FCT pool host and insert the same kind of "
        "leasable row -- only the machine-provisioning step differs."
    ),
)
@click.option(
    "--region",
    required=True,
    type=str,
    help=(
        "Lease/region code stamped on every new row (e.g. ``US-EAST-VA``, ``US-WEST-OR``) -- this is "
        "what the connector's region-filtered lease matches. For ``ovh_vps`` it is also the OVH "
        "datacenter the VPS is ordered in. For ``slice`` it is the lease-region label only (NOT the "
        "box's raw datacenter code)."
    ),
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help=(
        "[ovh_vps only] Repeatable ``KEY=VALUE`` tag attached to every freshly-provisioned VPS via the "
        "OVH IAM v2 tag system. Forwarded to the inner ``mngr create`` as ``MNGR_VPS_EXTRA_TAGS=k1=v1,k2=v2``. "
        "Example: ``--tag minds_env=alice --tag pool-owner=bob``."
    ),
)
@click.option(
    "--attributes",
    "attributes_json",
    required=True,
    help=(
        'Lease-attributes JSON for the new pool rows (e.g. \'{"repo_branch_or_tag":"main"}\'). For '
        "``slice`` the per-box size (``memory_gb`` / ``cpus``) is computed and stamped on top of these."
    ),
)
@click.option(
    "--workspace-dir",
    required=False,
    default=None,
    type=click.Path(exists=True),
    help=(
        "Path to the template (FCT) repo checkout to bake from. Required for ``ovh_vps``; for ``slice`` "
        "it defaults to $HOME/project/forever-claude-template."
    ),
)
@click.option(
    "--management-public-key-file",
    required=False,
    default=None,
    type=click.Path(exists=True),
    help=(
        "[ovh_vps only] Path to the management SSH public key installed on the VPS + container. Slices "
        "authorize the pool key from POOL_SSH_PRIVATE_KEY at carve time, so they do not use this."
    ),
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
        "[ovh_vps only] Force a fresh OVH VPS order instead of reclaiming a cancelled VPS. By default the OVH "
        "provider recycles a cancelled (still-billable) VPS when one is available; pass this to "
        "test the fresh-provision path. Sets MNGR__PROVIDERS__OVH__ENABLE_RECYCLE_CANCELLED=false "
        "on the inner `mngr create`."
    ),
)
@click.option(
    "--dry-run",
    "is_dry_run",
    is_flag=True,
    default=False,
    help="[slice only] Report placement + per-slice sizing; do not bake.",
)
def pool_create(
    count: int,
    backend: str,
    region: str,
    tags: tuple[str, ...],
    attributes_json: str,
    workspace_dir: str | None,
    management_public_key_file: str | None,
    database_url: str | None,
    mngr_source: str | None,
    is_recycle_enabled: bool,
    is_dry_run: bool,
) -> None:
    """Create pre-provisioned pool hosts on the chosen backend (OVH VPS or bare-metal slice)."""
    resolved_database_url = resolve_pool_database_url(database_url)
    try:
        parsed_attributes = _json.loads(attributes_json)
    except _json.JSONDecodeError as exc:
        logger.error("Invalid --attributes JSON: {}", exc)
        fail_with_json(f"Invalid --attributes JSON: {exc}", error_class="UsageError")
    if not isinstance(parsed_attributes, dict):
        fail_with_json("--attributes must be a JSON object", error_class="UsageError")

    # Slice backend: the machine is a lima VM on a registered box. Reject the
    # OVH-only flags, then hand off to the shared slice provisioning path (which
    # merges the per-box size onto these lease attributes and uses --region as the
    # row's lease-region label).
    if backend == "slice":
        if tags:
            fail_with_json("--tag is not applicable to --backend slice", error_class="UsageError")
        if management_public_key_file is not None:
            fail_with_json(
                "--management-public-key-file is not applicable to --backend slice "
                "(slices authorize the pool key from POOL_SSH_PRIVATE_KEY at carve time)",
                error_class="UsageError",
            )
        if not is_recycle_enabled:
            fail_with_json("--no-recycle is not applicable to --backend slice", error_class="UsageError")
        allocate_slices(
            count=count,
            lease_attributes=parsed_attributes,
            region=region,
            workspace_dir=workspace_dir,
            mngr_source=mngr_source,
            database_url=resolved_database_url,
            is_dry_run=is_dry_run,
        )
        return

    # OVH VPS backend.
    if is_dry_run:
        fail_with_json("--dry-run is only supported for --backend slice", error_class="UsageError")
    if not workspace_dir:
        fail_with_json("--workspace-dir is required for --backend ovh_vps", error_class="UsageError")
    if not management_public_key_file:
        fail_with_json("--management-public-key-file is required for --backend ovh_vps", error_class="UsageError")
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
        sync_mngr_into_template(Path(mngr_source), workspace_path)

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
    resolved_database_url = resolve_pool_database_url(database_url)
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
    resolved_database_url = resolve_pool_database_url(database_url)
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
