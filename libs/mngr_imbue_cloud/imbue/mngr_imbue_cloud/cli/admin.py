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
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never
from uuid import uuid4

import click
import psycopg2
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.bake.bake_source import BakeSourceError
from imbue.mngr_imbue_cloud.bake.bake_source import DEFAULT_FCT_REPO_URL
from imbue.mngr_imbue_cloud.bake.bake_source import merge_bake_identity_attributes
from imbue.mngr_imbue_cloud.bake.bake_source import resolved_bake_source
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
from imbue.mngr_imbue_cloud.cli.server import DEFAULT_SLICE_BAKE_CONCURRENCY
from imbue.mngr_imbue_cloud.cli.server import allocate_slices
from imbue.mngr_imbue_cloud.cli.server import destroy_slice_vm
from imbue.mngr_imbue_cloud.errors import RepoIdentityError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.slices.bare_metal_db import fetch_server_by_id
from imbue.mngr_ovh.client import build_ovh_client
from imbue.mngr_ovh.config import OvhProviderConfig
from imbue.mngr_ovh.iam_tags import MNGR_PROVIDER_TAG_KEY
from imbue.mngr_ovh.iam_tags import delete_tag
from imbue.mngr_ovh.iam_tags import get_vps_resource
from imbue.mngr_ovh.iam_tags import iam_region_code_for_endpoint
from imbue.mngr_ovh.iam_tags import vps_urn_for
from imbue.mngr_vps.primitives import VpsInstanceId

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
    ``mngr_vps.build_vps_tags`` and ``mngr_ovh.iam_tags.parse_extra_tags_env``
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
    is_deferred_install_wait_skipped: bool,
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
    # finish before we stop the services agent: stopping mid-apt corrupts dpkg. Dev bakes
    # may skip this wait to save a few minutes (the tradeoff is a possibly-incomplete baked
    # deferred-install -- fine for throwaway dev hosts).
    if is_deferred_install_wait_skipped:
        logger.warning(
            "Skipping deferred-install wait for {} (dev bake); its baked deferred-install may be incomplete",
            host_name,
        )
    else:
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
    "--from-tag",
    "from_tag",
    default=None,
    help=(
        "[production bake] Clone --repo-url at exactly this tag into a fresh temp dir and bake from it. "
        "Stamps repo_url=canonical(--repo-url) and repo_branch_or_tag=<tag>; the content provably equals the "
        "tag. Mutually exclusive with --workspace-dir; errors if <tag> is not a real tag."
    ),
)
@click.option(
    "--repo-url",
    "repo_url",
    default=DEFAULT_FCT_REPO_URL,
    help="[--from-tag only] Canonical repo to clone the tag from (default: the FCT remote).",
)
@click.option(
    "--workspace-dir",
    required=False,
    default=None,
    type=click.Path(exists=True),
    help=(
        "[dev bake] Bake content from this working tree (uncommitted changes included). Stamps "
        "repo_url=canonical(origin of the folder) and repo_branch_or_tag=<folder's current branch> "
        "(override with --repo-branch-or-tag). Mutually exclusive with --from-tag; errors without an origin."
    ),
)
@click.option(
    "--repo-branch-or-tag",
    "repo_branch_or_tag_override",
    default=None,
    help="[--workspace-dir only] Override the branch label stamped (default: the folder's current branch).",
)
@click.option(
    "--attributes",
    "attributes_json",
    required=False,
    default=None,
    help=(
        "Optional non-identity lease-attributes JSON for the new pool rows. The identity keys repo_url and "
        "repo_branch_or_tag are NOT allowed here -- they are derived from the bake source (--from-tag / "
        "--workspace-dir). For slice the per-box size (memory_gb / cpus) is computed and stamped automatically."
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
    "--server-id",
    "server_id",
    default=None,
    help=(
        "[slice only, required] The bare_metal_servers row id to bake the slices onto (from "
        "`admin server list`). Slice baking targets an explicitly-chosen, ready box -- it never "
        "auto-selects one."
    ),
)
@click.option(
    "--dry-run",
    "is_dry_run",
    is_flag=True,
    default=False,
    help="[slice only] Report placement + per-slice sizing; do not bake.",
)
@click.option(
    "--max-concurrency",
    "max_concurrency",
    type=int,
    default=DEFAULT_SLICE_BAKE_CONCURRENCY,
    show_default=True,
    help=(
        "[slice only] Max slices baked at once; the rest queue and start as slots free. "
        "Bounds box CPU/IO/network contention so each `mngr create` stays under its timeout."
    ),
)
@click.option(
    "--skip-deferred-install-wait",
    "is_deferred_install_wait_skipped",
    is_flag=True,
    default=False,
    help=(
        "[dev only] Don't wait for the FCT deferred-install (heavy apt + Playwright/Chromium) to "
        "finish before stopping the baked services agent. Saves a few minutes per bake, but the baked "
        "container's deferred-install may be left incomplete (stopping mid-apt can corrupt dpkg). Safe "
        "for dev/throwaway bakes; NEVER use for production pool hosts."
    ),
)
def pool_create(
    count: int,
    backend: str,
    region: str,
    tags: tuple[str, ...],
    from_tag: str | None,
    repo_url: str,
    workspace_dir: str | None,
    repo_branch_or_tag_override: str | None,
    attributes_json: str | None,
    management_public_key_file: str | None,
    database_url: str | None,
    mngr_source: str | None,
    is_recycle_enabled: bool,
    server_id: str | None,
    is_dry_run: bool,
    max_concurrency: int,
    is_deferred_install_wait_skipped: bool,
) -> None:
    """Create pre-provisioned pool hosts on the chosen backend (OVH VPS or bare-metal slice).

    The bake source -- exactly one of ``--from-tag`` (production, clones a tag) or
    ``--workspace-dir`` (dev, a working tree) -- determines the content baked and
    the canonical ``repo_url`` / ``repo_branch_or_tag`` stamped into each row, so
    the advertised identity always describes what is actually baked.
    """
    resolved_database_url = resolve_pool_database_url(database_url)
    parsed_attributes = _parse_optional_attributes_json(attributes_json)

    # Backend-specific flag validation, done before the (clone-heavy) source resolve.
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
        if not server_id:
            fail_with_json(
                "--server-id is required for --backend slice (the bare-metal box to bake onto; "
                "see `mngr imbue_cloud admin server list`)",
                error_class="UsageError",
            )
    elif backend == "ovh_vps":
        if is_dry_run:
            fail_with_json("--dry-run is only supported for --backend slice", error_class="UsageError")
        if server_id is not None:
            fail_with_json("--server-id is only supported for --backend slice", error_class="UsageError")
        if not management_public_key_file:
            fail_with_json("--management-public-key-file is required for --backend ovh_vps", error_class="UsageError")
        # Validate ``--tag`` shapes up front so we don't bake the first host and
        # then trip over a typo on the second one.
        try:
            build_extra_tags_env_value(tags)
        except click.UsageError as exc:
            fail_with_json(str(exc), error_class="UsageError")
    else:
        # ``--backend`` is a click.Choice, so this is unreachable in practice.
        fail_with_json(f"unknown --backend {backend!r}", error_class="UsageError")

    # Resolve the bake source and derive the identity attributes to stamp. The
    # context manager cleans up any temp clone (--from-tag) on exit; both the
    # dry-run report and the real bake go through it, so they cannot disagree.
    try:
        with resolved_bake_source(
            from_tag=from_tag,
            workspace_dir=workspace_dir,
            repo_url=repo_url,
            repo_branch_or_tag_override=repo_branch_or_tag_override,
        ) as bake_source:
            attributes = merge_bake_identity_attributes(parsed_attributes, bake_source)
            if backend == "slice":
                # ``server_id`` presence is enforced above for the slice backend.
                assert server_id is not None
                allocate_slices(
                    count=count,
                    server_id=server_id,
                    lease_attributes=attributes,
                    region=region,
                    workspace_dir=bake_source.workspace_dir,
                    mngr_source=mngr_source,
                    database_url=resolved_database_url,
                    is_dry_run=is_dry_run,
                    is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
                    max_concurrency=max_concurrency,
                )
            else:
                # The ovh_vps branch above already rejected a missing key; assert it
                # so the type checker sees the non-None value the helper requires.
                assert management_public_key_file is not None
                _create_ovh_vps_pool_hosts(
                    count=count,
                    region=region,
                    tags=tags,
                    attributes=attributes,
                    workspace_dir=bake_source.workspace_dir,
                    management_public_key_file=management_public_key_file,
                    database_url=resolved_database_url,
                    mngr_source=mngr_source,
                    is_recycle_enabled=is_recycle_enabled,
                    is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
                )
    except (BakeSourceError, RepoIdentityError) as exc:
        fail_with_json(str(exc), error_class="UsageError")


def _parse_optional_attributes_json(attributes_json: str | None) -> dict[str, Any]:
    """Parse the optional --attributes JSON object, defaulting to empty when absent."""
    if not attributes_json:
        return {}
    try:
        parsed = _json.loads(attributes_json)
    except _json.JSONDecodeError as exc:
        logger.error("Invalid --attributes JSON: {}", exc)
        fail_with_json(f"Invalid --attributes JSON: {exc}", error_class="UsageError")
    if not isinstance(parsed, dict):
        fail_with_json("--attributes must be a JSON object", error_class="UsageError")
    return parsed


def _create_ovh_vps_pool_hosts(
    *,
    count: int,
    region: str,
    tags: tuple[str, ...],
    attributes: dict[str, Any],
    workspace_dir: Path,
    management_public_key_file: str,
    database_url: str,
    mngr_source: str | None,
    is_recycle_enabled: bool,
    is_deferred_install_wait_skipped: bool,
) -> None:
    """Bake ``count`` OVH-VPS pool hosts from ``workspace_dir`` with the derived attributes."""
    management_public_key = Path(management_public_key_file).read_text().strip()
    if not management_public_key:
        fail_with_json("Management public key file is empty", error_class="UsageError")

    if mngr_source is not None:
        sync_mngr_into_template(Path(mngr_source), workspace_dir)

    logger.info(
        "Creating {} pool host(s) with region={}, attributes={}, tags={}",
        count,
        region,
        attributes,
        list(tags),
    )

    success_count = 0
    failures: list[str] = []
    for i in range(1, count + 1):
        logger.info("[{}/{}]", i, count)
        try:
            is_success = _create_single_pool_host(
                workspace_dir=workspace_dir,
                attributes=attributes,
                management_public_key=management_public_key,
                database_url=database_url,
                region=region,
                extra_tags=tags,
                is_recycle_enabled=is_recycle_enabled,
                is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
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


# ``pool_hosts.backend_kind`` value for bare-metal slices (lima VMs). OVH-VPS rows
# carry the column's 'ovh_vps' default, so destroy treats anything that is not
# 'slice' (including legacy/None rows written before the column existed) as the OVH
# teardown path.
_SLICE_BACKEND_KIND: Final[str] = "slice"


class PoolHostUnderlyingTeardown(UpperCaseStrEnum):
    """Which underlying machine a ``pool destroy`` tears down before dropping the row."""

    OVH_VPS = auto()
    SLICE_VM = auto()
    NONE = auto()


@pure
def resolve_underlying_teardown(*, backend_kind: str | None, is_skip_requested: bool) -> PoolHostUnderlyingTeardown:
    """Decide what underlying teardown a pool host destroy performs, from the row's backend.

    ``--skip-vps-cancel`` always wins (drop the row only). Otherwise a slice tears down
    its lima VM and an OVH-VPS row (including legacy/None backends) cancels its VPS --
    mirroring the backend branch in ``pool create`` so a slice is never left stranded.
    """
    if is_skip_requested:
        return PoolHostUnderlyingTeardown.NONE
    if backend_kind == _SLICE_BACKEND_KIND:
        return PoolHostUnderlyingTeardown.SLICE_VM
    return PoolHostUnderlyingTeardown.OVH_VPS


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


def _destroy_slice_pool_host_vm(
    *,
    conn: Any,
    pool_host_id: str,
    bare_metal_server_id: str | None,
    lima_instance_name: str | None,
) -> None:
    """Destroy a slice pool host's lima VM on its bare-metal box (the slice counterpart of VPS cancel).

    Resolves the box from ``bare_metal_server_id`` and tears down the
    ``lima_instance_name`` VM via the pool key (POOL_SSH_PRIVATE_KEY). Raises -- so
    the caller keeps the row and the teardown stays retryable -- when the slice row
    lacks its lima identifiers or the referenced box no longer exists.
    """
    if not bare_metal_server_id or not lima_instance_name:
        fail_with_json(
            f"Slice row {pool_host_id} is missing its bare_metal_server_id / lima_instance_name; "
            "cannot locate the VM to destroy. Pass --skip-vps-cancel to drop the row only.",
            error_class="UnsafeDelete",
        )
    server = fetch_server_by_id(conn, BareMetalServerDbId(bare_metal_server_id))
    if server is None:
        fail_with_json(
            f"Slice row {pool_host_id} references bare_metal_server {bare_metal_server_id}, which no "
            "longer exists; cannot reach the box. Pass --skip-vps-cancel to drop the row only.",
            error_class="UnsafeDelete",
        )
    destroy_slice_vm(server=server, lima_instance_name=lima_instance_name)


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
        "Only drop the DB row; do NOT tear down the underlying machine (cancel the "
        "OVH VPS for an ovh_vps row, or destroy the lima VM for a slice row). Use "
        "exclusively when the machine is already gone -- otherwise the default path "
        "tears it down so no billing/slot orphan is left behind."
    ),
)
def pool_destroy(pool_host_id: str, database_url: str | None, force: bool, skip_vps_cancel: bool) -> None:
    """Remove a pool_hosts row, tearing down its underlying machine first (full teardown).

    The teardown mirrors the row's backend (just as ``pool create`` branches on
    ``--backend``): an ``ovh_vps`` row cancels its OVH VPS (strip per-lease tags +
    ``deleteAtExpiration=True``), while a ``slice`` row destroys its lima VM on the
    bare-metal box (freeing the slot). Either runs *before* the row is deleted, so a
    failure keeps the row and the teardown stays retryable -- never a stranded VPS or
    slice VM. Pass ``--skip-vps-cancel`` only when the machine is already gone.
    Teardown needs creds in the environment (the minds ``pool destroy`` wrapper
    injects them from Vault: OVH AK/AS/CK for ovh_vps, POOL_SSH_PRIVATE_KEY for slice).
    """
    resolved_database_url = resolve_pool_database_url(database_url)
    conn = psycopg2.connect(resolved_database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, vps_address, backend_kind, bare_metal_server_id, lima_instance_name "
                "FROM pool_hosts WHERE id = %s",
                (pool_host_id,),
            )
            row = cur.fetchone()
            if row is None:
                fail_with_json(f"No pool_hosts row with id {pool_host_id}", error_class="NotFound")
            status, vps_address, backend_kind, bare_metal_server_id, lima_instance_name = row
            if status != "released" and not force:
                fail_with_json(
                    f"Row {pool_host_id} is in status '{status}'; pass --force to delete anyway",
                    error_class="UnsafeDelete",
                )
        # Tear the underlying machine down BEFORE deleting the row: if it fails we
        # keep the row so the teardown stays retryable (no silent orphan). The
        # backend dictates how -- mirroring pool create's backend branch.
        teardown = resolve_underlying_teardown(backend_kind=backend_kind, is_skip_requested=skip_vps_cancel)
        match teardown:
            case PoolHostUnderlyingTeardown.SLICE_VM:
                _destroy_slice_pool_host_vm(
                    conn=conn,
                    pool_host_id=pool_host_id,
                    bare_metal_server_id=bare_metal_server_id,
                    lima_instance_name=lima_instance_name,
                )
            case PoolHostUnderlyingTeardown.OVH_VPS:
                if not vps_address:
                    fail_with_json(
                        f"Row {pool_host_id} has no vps_address; cannot cancel its VPS. "
                        "Pass --skip-vps-cancel if the VPS is already gone.",
                        error_class="UnsafeDelete",
                    )
                _cancel_pool_host_vps(vps_address)
            case PoolHostUnderlyingTeardown.NONE:
                pass
            case _ as unreachable:
                assert_never(unreachable)
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pool_hosts WHERE id = %s", (pool_host_id,))
    finally:
        conn.close()
    emit_json(
        {
            "deleted": True,
            "pool_host_id": pool_host_id,
            "backend_kind": backend_kind,
            "vps_cancelled": teardown == PoolHostUnderlyingTeardown.OVH_VPS,
            "slice_vm_destroyed": teardown == PoolHostUnderlyingTeardown.SLICE_VM,
        }
    )
