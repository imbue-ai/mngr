"""`mngr imbue_cloud admin pool ...` -- operator-only pool provisioning.

``pool create`` bakes pre-provisioned pool hosts as lima-VM "slices" carved on one
of our registered bare-metal boxes (the shared implementation is
``cli.server.allocate_slices``). The bake writes a leasable row to the connector's
Neon ``pool_hosts`` table.

Provider-generic by design: this command has no knowledge of minds environments;
that's the caller's responsibility (the ``minds pool`` wrapper threads the owning
env name through ``--slice-env-name``).

Authentication: this command talks to Neon directly via ``DATABASE_URL``. It does
NOT use the operator's SuperTokens session; the connector is not involved in pool
provisioning at all.
"""

import json as _json
from typing import Any
from typing import Final

import click
import psycopg2
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_imbue_cloud.bake.bake_source import BakeSourceError
from imbue.mngr_imbue_cloud.bake.bake_source import DEFAULT_FCT_REPO_URL
from imbue.mngr_imbue_cloud.bake.bake_source import merge_bake_identity_attributes
from imbue.mngr_imbue_cloud.bake.bake_source import resolved_bake_source
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json
from imbue.mngr_imbue_cloud.cli._common import resolve_pool_database_url
from imbue.mngr_imbue_cloud.cli.server import DEFAULT_SLICE_BAKE_CONCURRENCY
from imbue.mngr_imbue_cloud.cli.server import allocate_slices
from imbue.mngr_imbue_cloud.cli.server import destroy_slice_vm
from imbue.mngr_imbue_cloud.cli.server import tear_down_unleased_slices
from imbue.mngr_imbue_cloud.errors import RepoIdentityError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import KNOWN_OVH_US_REGIONS
from imbue.mngr_imbue_cloud.slices.bare_metal_db import fetch_server_by_id


@click.group(name="admin")
def admin() -> None:
    """Operator-only commands."""


@admin.group(name="pool")
def pool() -> None:
    """Pool host provisioning (bare-metal slices + Neon)."""


@pool.command(name="create")
@click.option("--count", required=True, type=int, help="Number of pool hosts to create")
@click.option(
    "--region",
    required=True,
    type=str,
    help=(
        "Lease-region label stamped on every new row (e.g. ``US-EAST-VA``, ``US-WEST-OR``) -- this is "
        "what the connector's region-filtered lease matches. It is the lease-region label only (NOT the "
        "box's raw datacenter code)."
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
        "--workspace-dir). The per-box size (memory_gb / cpus) is computed and stamped automatically."
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
    "--server-id",
    "server_id",
    default=None,
    help=(
        "[required] The bare_metal_servers row id to bake the slices onto (from "
        "`admin server list`). Slice baking targets an explicitly-chosen, ready box -- it never "
        "auto-selects one."
    ),
)
@click.option(
    "--slice-env-name",
    "slice_env_name",
    default=None,
    help=(
        "Owning environment name stamped into each slice's lima instance + disk names "
        "(mngr-slice-<env>-<host-hex>). Lets multiple dev envs share one bare-metal box: occupancy is read "
        "from the box, and the post-bake reap only ever touches this env's own slices. Usually forwarded by "
        "`minds pool create` from the activated env; omit only for legacy un-stamped baking."
    ),
)
@click.option(
    "--dry-run",
    "is_dry_run",
    is_flag=True,
    default=False,
    help="Report placement + per-slice sizing; do not bake.",
)
@click.option(
    "--max-concurrency",
    "max_concurrency",
    type=int,
    default=DEFAULT_SLICE_BAKE_CONCURRENCY,
    show_default=True,
    help=(
        "Max slices baked at once; the rest queue and start as slots free. "
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
    region: str,
    from_tag: str | None,
    repo_url: str,
    workspace_dir: str | None,
    repo_branch_or_tag_override: str | None,
    attributes_json: str | None,
    database_url: str | None,
    mngr_source: str | None,
    server_id: str | None,
    slice_env_name: str | None,
    is_dry_run: bool,
    max_concurrency: int,
    is_deferred_install_wait_skipped: bool,
) -> None:
    """Create pre-provisioned bare-metal slice pool hosts.

    The bake source -- exactly one of ``--from-tag`` (production, clones a tag) or
    ``--workspace-dir`` (dev, a working tree) -- determines the content baked and
    the canonical ``repo_url`` / ``repo_branch_or_tag`` stamped into each row, so
    the advertised identity always describes what is actually baked.
    """
    # The region is the lease-region label the connector region-matches at lease
    # time (e.g. US-EAST-VA), NOT a box's raw OVH datacenter code (e.g. 'vin',
    # which `admin server list` prints). Stamping a datacenter code onto the row
    # would make every baked host permanently unleasable: the create form only
    # ever requests a lease label and the connector's region filter is an exact,
    # never-relaxed string match. Reject anything outside the known lease regions
    # up front, before any (clone-heavy) bake work.
    if region not in KNOWN_OVH_US_REGIONS:
        fail_with_json(
            f"--region {region!r} is not a known lease region. Pass one of "
            f"{sorted(KNOWN_OVH_US_REGIONS)} (the lease-region label, e.g. US-EAST-VA) -- "
            "NOT the box's OVH datacenter code (e.g. 'vin' from `admin server list`).",
            error_class="UsageError",
        )

    resolved_database_url = resolve_pool_database_url(database_url)
    parsed_attributes = _parse_optional_attributes_json(attributes_json)

    if not server_id:
        fail_with_json(
            "--server-id is required (the bare-metal box to bake onto; see `mngr imbue_cloud admin server list`)",
            error_class="UsageError",
        )

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
            # ``server_id`` presence is enforced above.
            assert server_id is not None
            allocate_slices(
                count=count,
                server_id=server_id,
                lease_attributes=attributes,
                region=region,
                env_name=slice_env_name,
                workspace_dir=bake_source.workspace_dir,
                mngr_source=mngr_source,
                # A --from-tag bake must keep the tag's own vendored mngr (byte-for-byte
                # release content); only --workspace-dir / --mngr-source override it.
                is_from_tag=from_tag is not None,
                database_url=resolved_database_url,
                is_dry_run=is_dry_run,
                is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
                max_concurrency=max_concurrency,
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


# Every pool_hosts column, in a stable display order, used to build BOTH the
# `pool list` SELECT and the keys of each emitted JSON row -- so the two can
# never drift. Hand-maintaining a subset is what silently dropped region and the
# slice identifiers (bare_metal_server_id / lima_instance_name / lima_disk_name)
# from the output. emit_json serialises the UUID and datetime values via its
# default=str, so no per-column coercion is needed.
_POOL_HOST_LIST_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "host_name",
    "status",
    "region",
    "attributes",
    "vps_address",
    "vps_instance_id",
    "agent_id",
    "host_id",
    "ssh_user",
    "ssh_port",
    "container_ssh_port",
    "bare_metal_server_id",
    "lima_instance_name",
    "lima_disk_name",
    "leased_to_user",
    "leased_at",
    "released_at",
    "created_at",
)


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
            cur.execute(f"SELECT {', '.join(_POOL_HOST_LIST_COLUMNS)} FROM pool_hosts ORDER BY created_at DESC")
            rows = cur.fetchall()
    finally:
        conn.close()
    emit_json([dict(zip(_POOL_HOST_LIST_COLUMNS, row, strict=True)) for row in rows])


def _destroy_slice_pool_host_vm(
    *,
    conn: Any,
    pool_host_id: str,
    bare_metal_server_id: str | None,
    lima_instance_name: str | None,
) -> None:
    """Destroy a slice pool host's lima VM on its bare-metal box (before dropping the row).

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
    "is_vm_teardown_skipped",
    is_flag=True,
    default=False,
    help=(
        "Only drop the DB row; do NOT destroy the underlying slice lima VM. Use "
        "exclusively when the VM is already gone -- otherwise the default path tears "
        "it down so no box slot is left occupied."
    ),
)
def pool_destroy(pool_host_id: str, database_url: str | None, force: bool, is_vm_teardown_skipped: bool) -> None:
    """Remove a pool_hosts row, destroying its slice lima VM first (full teardown).

    The slice VM on the bare-metal box is destroyed (freeing the slot) *before* the
    row is deleted, so a failure keeps the row and the teardown stays retryable --
    never a stranded slice VM. Pass ``--skip-vps-cancel`` only when the VM is already
    gone. Teardown needs POOL_SSH_PRIVATE_KEY in the environment to SSH the box (the
    minds ``pool destroy`` wrapper injects it from Vault).
    """
    resolved_database_url = resolve_pool_database_url(database_url)
    conn = psycopg2.connect(resolved_database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, bare_metal_server_id, lima_instance_name FROM pool_hosts WHERE id = %s",
                (pool_host_id,),
            )
            row = cur.fetchone()
            if row is None:
                fail_with_json(f"No pool_hosts row with id {pool_host_id}", error_class="NotFound")
            status, bare_metal_server_id, lima_instance_name = row
            if status != "released" and not force:
                fail_with_json(
                    f"Row {pool_host_id} is in status '{status}'; pass --force to delete anyway",
                    error_class="UnsafeDelete",
                )
        # Tear the slice VM down BEFORE deleting the row: if it fails we keep the row
        # so the teardown stays retryable (no silent slot orphan).
        if not is_vm_teardown_skipped:
            _destroy_slice_pool_host_vm(
                conn=conn,
                pool_host_id=pool_host_id,
                bare_metal_server_id=bare_metal_server_id,
                lima_instance_name=lima_instance_name,
            )
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pool_hosts WHERE id = %s", (pool_host_id,))
    finally:
        conn.close()
    emit_json(
        {
            "deleted": True,
            "pool_host_id": pool_host_id,
            "slice_vm_destroyed": not is_vm_teardown_skipped,
        }
    )


@pool.command(name="teardown-slices")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=(
        "Neon PostgreSQL direct connection string for the pool DB. Defaults to "
        "MINDS_HOST_POOL_DSN env var, or the activated minds env's secrets.toml "
        "NEON_HOST_POOL_DSN field. Pass explicitly when operating outside an activated env."
    ),
)
def pool_teardown_slices(database_url: str | None) -> None:
    """Tear down every unleased slice VM in the pool DB and drop its row.

    Used by ``minds env destroy`` (before the per-env DB is deleted) so the env's
    baked-but-unleased pool slices don't leak their VMs on the shared bare-metal
    boxes. Leased slices are excluded -- they are torn down via their agent's release
    path. Needs POOL_SSH_PRIVATE_KEY in the environment to SSH the boxes (the minds
    wrapper injects it from Vault). Idempotent per VM; fails (non-zero) if any box
    could not be reached, so the caller can stop rather than silently leak.
    """
    resolved_database_url = resolve_pool_database_url(database_url)
    result = tear_down_unleased_slices(resolved_database_url)
    emit_json(result)


_KEYSCAN_TIMEOUT_SECONDS: Final[int] = 15

# SELECT pool rows still missing either pinned host key (pre-host-key-column bakes).
_SELECT_POOL_HOSTS_MISSING_KEYS_SQL: Final[str] = (
    "SELECT id, vps_address, ssh_port, container_ssh_port, outer_host_public_key, container_host_public_key "
    "FROM pool_hosts WHERE outer_host_public_key IS NULL OR container_host_public_key IS NULL"
)
_SELECT_BOXES_MISSING_KEY_SQL: Final[str] = (
    "SELECT id, public_address FROM bare_metal_servers "
    "WHERE box_host_public_key IS NULL AND public_address IS NOT NULL"
)


def _keyscan_host_public_key(host: str, port: int) -> str | None:
    """One-time TOFU scan of a host's ed25519 sshd key, for the migration backfill only.

    Returns ``"ssh-ed25519 <base64>"`` or None on failure. This is the single
    sanctioned trust-on-first-use in the system; all steady-state SSH pins a
    recorded key.
    """
    cg = ConcurrencyGroup(name="keyscan")
    with cg:
        result = cg.run_process_to_completion(
            command=["ssh-keyscan", "-t", "ed25519", "-T", str(_KEYSCAN_TIMEOUT_SECONDS), "-p", str(port), host],
            timeout=float(_KEYSCAN_TIMEOUT_SECONDS + 5),
            is_checked_after=False,
        )
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        # ssh-keyscan prints "<hostspec> ssh-ed25519 <base64>"; we want the key only.
        if len(parts) >= 3 and parts[1] == "ssh-ed25519":
            return f"{parts[1]} {parts[2]}"
    return None


@pool.command(name="backfill-host-keys")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=(
        "Neon PostgreSQL direct connection string for the pool DB. Defaults to "
        "MINDS_HOST_POOL_DSN env var, or the activated minds env's secrets.toml "
        "NEON_HOST_POOL_DSN field. Pass explicitly when operating outside an activated env."
    ),
)
def pool_backfill_host_keys(database_url: str | None) -> None:
    """One-time: keyscan + record SSH host public keys for pre-existing pool rows and boxes.

    The single sanctioned trust-on-first-use in the system, used ONLY to migrate
    rows baked before the host-key columns existed. Run once after deploying the
    host-key-pinning version of the connector; afterward leasing and teardown
    enforce strict pinning with no scan fallback. Idempotent: rows that already have
    keys are skipped, and a row whose host cannot be scanned is left null (logged)
    for a later re-run.
    """
    resolved_database_url = resolve_pool_database_url(database_url)
    conn = psycopg2.connect(resolved_database_url)
    # Keyscans are slow network ops; autocommit each UPDATE rather than hold one
    # long transaction open across them.
    conn.autocommit = True
    pool_updated = 0
    box_updated = 0
    skipped: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute(_SELECT_POOL_HOSTS_MISSING_KEYS_SQL)
            pool_rows = cur.fetchall()
        for row_id, vps_address, ssh_port, container_ssh_port, outer_key, container_key in pool_rows:
            new_outer = outer_key or _keyscan_host_public_key(vps_address, ssh_port)
            new_container = container_key or _keyscan_host_public_key(vps_address, container_ssh_port)
            if new_outer and new_container:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE pool_hosts SET outer_host_public_key = %s, container_host_public_key = %s WHERE id = %s",
                        (new_outer, new_container, str(row_id)),
                    )
                pool_updated += 1
            else:
                skipped.append(f"pool host {row_id} ({vps_address})")
                logger.warning("Could not keyscan host keys for pool host {} ({}); left null", row_id, vps_address)

        with conn.cursor() as cur:
            cur.execute(_SELECT_BOXES_MISSING_KEY_SQL)
            box_rows = cur.fetchall()
        for server_id, public_address in box_rows:
            box_key = _keyscan_host_public_key(public_address, 22)
            if box_key:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE bare_metal_servers SET box_host_public_key = %s, updated_at = NOW() WHERE id = %s",
                        (box_key, str(server_id)),
                    )
                box_updated += 1
            else:
                skipped.append(f"box {server_id} ({public_address})")
                logger.warning("Could not keyscan box key for server {} ({}); left null", server_id, public_address)
    finally:
        conn.close()
    emit_json({"pool_hosts_backfilled": pool_updated, "boxes_backfilled": box_updated, "skipped": skipped})
