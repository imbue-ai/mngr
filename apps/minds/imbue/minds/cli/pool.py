"""``minds pool {create,list,destroy}`` -- env-aware wrapper around ``mngr imbue_cloud admin pool``.

Responsibility split:

* ``mngr imbue_cloud admin pool create`` (in ``libs/mngr_imbue_cloud``) is the
  provider-generic host-creation step. It accepts a required ``--region`` and a
  ``--server-id`` (the bare-metal box to carve the slice on) and knows nothing
  about minds environments.
* This module is the env-aware layer. From the activated minds env
  (``MINDS_ROOT_NAME``) it:
    1. forwards ``--slice-env-name <env-name>`` (stamped into each slice's lima
       names, so a shared box can attribute the slice to this env and the
       post-bake reap only touches this env's own slices);
    2. derives the host_pool DSN and the pool SSH private key from the activated
       tier's Vault entries and injects ``POOL_SSH_PRIVATE_KEY`` into the admin
       subprocess env -- the same key the connector loads from its
       ``pool-ssh-<tier>`` Modal Secret, so the key the slice bake authorizes on
       the VM matches the one the connector SSHes with at lease/release time.
  All other admin flags (``--count`` / ``--attributes`` / ``--workspace-dir``
  / ``--database-url`` / ``--mngr-source``) forward 1:1.

Transport is subprocess (``mngr imbue_cloud admin pool ...``) to match the
rest of the minds env CLI's mngr invocations and to keep the minds -> mngr
dependency direction unchanged.

The argument-construction logic (``build_*_args``) is split out from the
click commands so unit tests can verify the env-name injection + flag
forwarding behaviour without standing up a fake subprocess runner.
"""

import os
import shlex
import sys
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final

import click
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.minds.cli._activated_env import PRODUCTION_ENV_NAME
from imbue.minds.cli._activated_env import STAGING_ENV_NAME
from imbue.minds.cli._activated_env import require_activated_env_name
from imbue.minds.cli._activated_env import tier_for_env_name
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.utils.secret_redaction import redact_secret_flag_values

# Hard cap on the admin pool-create subprocess. Generous (12h) so a large bulk bake
# (e.g. `--count 20` in waves of `--max-concurrency`, slow on a loaded box) is never
# killed mid-run. If it ever does fire, the slice backend reaps its orphans on
# SIGTERM, but the point of 12h is to not hit it in normal operation.
_POOL_COMMAND_TIMEOUT_SECONDS: Final[int] = 43200

# Flags whose values are secrets and must be masked when the admin command is
# rendered into the "Running: ..." log line. ``--database-url`` carries the
# Neon pool DSN (username + password); leaking it into logs/terminals is the
# exact issue this redaction closes.
_SECRET_BEARING_FLAGS: Final[tuple[str, ...]] = ("--database-url",)

# Vault key the pool management SSH private key lives under (per
# host-pool-setup.md step 2). The connector deploys with this private key pushed
# to a Modal Secret; the slice bake authorizes the matching public key on the VM.
_POOL_MGMT_PRIVATE_KEY_VAULT_FIELD: Final[str] = "POOL_SSH_PRIVATE_KEY"
# Vault field (under ``<vault_prefix>/neon``) holding the pooled host_pool DSN.
_POOL_DSN_VAULT_FIELD: Final[str] = "DATABASE_URL"
# Shared ``--database-url`` help text for every env-aware admin wrapper command
# (pool create / list / destroy here, and the ``minds server`` commands). Hoisted to
# one constant so the subcommands' ``--help`` output can't drift.
DATABASE_URL_HELP: Final[str] = (
    "Neon PostgreSQL connection string for the pool DB. Optional: for "
    "staging/production it is read from Vault (secrets/minds/<tier>/neon); "
    "for dev/ci it auto-resolves from the activated env's secrets.toml. "
    "Pass explicitly only when overriding."
)
# Env var the admin slice path reads the pool management private key from (see
# ``mngr_imbue_cloud.cli.server._pool_private_key_path``). The slice backend needs
# the private key itself to SSH the box and carve the lima VM.
POOL_PRIVATE_KEY_ENV_VAR: Final[str] = "POOL_SSH_PRIVATE_KEY"


def build_create_admin_args(
    *,
    env_name: str,
    count: int,
    region: str,
    from_tag: str | None,
    repo_url: str | None,
    workspace_dir: str | None,
    repo_branch_or_tag_override: str | None,
    attributes_json: str | None,
    database_url: str | None,
    mngr_source: str | None,
    is_dry_run: bool,
    is_deferred_install_wait_skipped: bool,
    server_id: str | None = None,
    max_concurrency: int | None = None,
) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool create`` argv from minds-side inputs.

    Forwards ``--slice-env-name <env_name>`` (stamped into each slice's lima names,
    so a shared box can attribute the slice to this env). Every other user-supplied
    flag forwards verbatim. Split out from the click command so tests can exercise
    the wiring without faking a subprocess.

    The bake source is exactly one of ``--from-tag`` (production, clones a tag)
    or ``--workspace-dir`` (dev, a working tree); the admin CLI derives the
    canonical ``repo_url`` / ``repo_branch_or_tag`` from it, so ``--attributes``
    carries only non-identity attributes (and may be omitted).

    ``--database-url`` is forwarded only when ``database_url`` is non-None.
    The caller (``pool_create`` via :func:`resolve_host_pool_dsn`) supplies a
    Vault-resolved DSN for staging / production and None for dev / ci; when
    None is passed through here the admin CLI auto-resolves the DSN from the
    activated minds env's ``secrets.toml`` (which the deploy wrote).

    ``--server-id`` (the explicitly-chosen bare-metal box), ``--dry-run`` (when
    ``is_dry_run`` is True), and ``--max-concurrency`` (when non-None) are
    forwarded only when set.
    """
    args = [
        "create",
        "--count",
        str(count),
        "--region",
        region,
    ]
    if from_tag is not None:
        args.extend(["--from-tag", from_tag])
    if repo_url is not None:
        args.extend(["--repo-url", repo_url])
    if workspace_dir is not None:
        args.extend(["--workspace-dir", workspace_dir])
    if repo_branch_or_tag_override is not None:
        args.extend(["--repo-branch-or-tag", repo_branch_or_tag_override])
    if attributes_json is not None:
        args.extend(["--attributes", attributes_json])
    if database_url is not None:
        args.extend(["--database-url", database_url])
    if mngr_source is not None:
        args.extend(["--mngr-source", mngr_source])
    # Stamp the owning env into each slice's lima names so multiple dev envs can
    # share one bare-metal box (occupancy read from the box; reap scoped to this env).
    args.extend(["--slice-env-name", env_name])
    if server_id is not None:
        args.extend(["--server-id", server_id])
    if is_dry_run:
        args.append("--dry-run")
    if max_concurrency is not None:
        args.extend(["--max-concurrency", str(max_concurrency)])
    if is_deferred_install_wait_skipped:
        args.append("--skip-deferred-install-wait")
    return args


def build_teardown_slices_admin_args(*, database_url: str | None) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool teardown-slices`` argv.

    Forwards ``--database-url`` only when non-None (dev auto-resolves it from the
    activated env's secrets.toml; staging/production pass the Vault-resolved DSN).
    """
    args = ["teardown-slices"]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    return args


def tear_down_env_pool_slices(env_name: str) -> None:
    """Tear down the env's unleased pool slices on their boxes before the env's DB is deleted.

    Resolves the pool SSH key (Vault) + host_pool DSN exactly like ``pool create``,
    then shells to ``mngr imbue_cloud admin pool teardown-slices``. Leased slices are
    left to their agent's release path. A missing pool SSH key is a bad state, not a
    "nothing to clean up" signal -- it raises (failing the destroy) so we never
    silently leak the env's slice VMs; a genuine teardown failure (an unreachable
    box) likewise raises rather than leaking.
    """
    try:
        pool_private_key = read_pool_private_key_from_vault(env_name)
    except VaultReadError as exc:
        raise click.ClickException(
            f"Could not read the pool SSH private key from Vault for env '{env_name}': {exc}"
        ) from exc
    database_url = resolve_host_pool_dsn(env_name, None)
    args = build_teardown_slices_admin_args(database_url=database_url)
    raise_on_admin_command_failure(
        "pool",
        "teardown-slices",
        run_imbue_cloud_admin_command("pool", args, extra_env={POOL_PRIVATE_KEY_ENV_VAR: pool_private_key}),
    )


def build_list_admin_args(*, database_url: str | None) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool list`` argv.

    ``--database-url`` forwarded only when explicitly supplied; see
    :func:`build_create_admin_args`.
    """
    args = ["list"]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    return args


def build_backfill_host_keys_admin_args(*, database_url: str | None) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool backfill-host-keys`` argv.

    ``--database-url`` forwarded only when explicitly supplied; see
    :func:`build_create_admin_args`.
    """
    args = ["backfill-host-keys"]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    return args


def build_destroy_admin_args(
    *,
    pool_host_ids: Sequence[str],
    database_url: str | None,
    is_leased_destroy_allowed: bool,
    is_row_drop_only: bool,
    max_concurrency: int | None,
) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool destroy`` argv (all ids in one invocation)."""
    args = ["destroy", *pool_host_ids]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    if is_leased_destroy_allowed:
        args.append("--force")
    if is_row_drop_only:
        args.append("--drop-row-only")
    if max_concurrency is not None:
        args.extend(["--max-concurrency", str(max_concurrency)])
    return args


def _stream_subprocess_line(line: str, is_stdout: bool) -> None:
    """Mirror a child-process line to our stderr in real time.

    Match the line-streaming helper in ``mngr_imbue_cloud.cli.admin``:
    we want to faithfully echo the inner ``mngr imbue_cloud admin pool``
    output without loguru's timestamp/level prefix, so a multi-host bake
    isn't a silent black box. ``logger.info`` would distort the format;
    ``write_human_line`` is for one-shot status messages, not streamed
    subprocess output.
    """
    suffix = "" if line.endswith("\n") else "\n"
    sys.stderr.write(line + suffix)
    sys.stderr.flush()


def merge_extra_env_into_subprocess_env(
    *, shell_env: Mapping[str, str], extra_env: Mapping[str, str]
) -> dict[str, str]:
    """Build the subprocess env: start from ``shell_env``, then layer ``extra_env`` on top.

    Injects the activated tier's ``POOL_SSH_PRIVATE_KEY`` (resolved from Vault) into
    the admin subprocess without mutating the parent process's environment. The
    Vault value from the activated tier wins over whatever the operator may have
    lying around in their shell. The operator's mental model when running
    ``minds pool create`` (with an activated env) is "this provisions hosts for the
    active tier" -- so the active tier's secrets are the source of truth, not a stale
    value that might still be exported from a different tier's session last week.

    Pure function so the precedence rule is testable without a fake
    subprocess runner or a fake Vault.
    """
    merged = dict(shell_env)
    merged.update(extra_env)
    return merged


def read_pool_private_key_from_vault(
    env_name: str,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> str:
    """Read the activated tier's pool management private key PEM from Vault.

    Reads ``<vault_path_prefix>/pool-ssh/POOL_SSH_PRIVATE_KEY`` -- the same entry
    ``minds env deploy`` pushes into the ``pool-ssh-<tier>`` Modal Secret the
    connector loads, so the key the slice bake authorizes on the VM matches the one
    the connector SSHes with at lease/release time. The slice backend needs the
    private key itself to SSH the box and carve the lima VM.

    Raises ``click.ClickException`` if the entry lacks the private-key field.
    Raises ``VaultReadError`` for any underlying Vault read failure.
    """
    tier = tier_for_env_name(env_name)
    deploy_config = load_deploy_config(tier)
    vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    secret = read_vault_kv(VaultPath(f"{vault_prefix}/pool-ssh"), parent_concurrency_group=parent_cg)
    private_key = secret.get(_POOL_MGMT_PRIVATE_KEY_VAULT_FIELD, "")
    if not private_key:
        raise click.ClickException(
            f"Vault entry {vault_prefix}/pool-ssh is missing {_POOL_MGMT_PRIVATE_KEY_VAULT_FIELD!r}; "
            "see apps/minds/docs/host-pool-setup.md step 2 for the schema."
        )
    return private_key


def resolve_host_pool_dsn(
    env_name: str,
    explicit_database_url: str | None,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> str | None:
    """Return the host_pool DSN to forward to the admin command, or None.

    Precedence: an explicit ``--database-url`` always wins. Otherwise the shared
    tiers (``staging`` / ``production``) keep no local ``secrets.toml``, so their
    DSN is read from the tier's ``<vault_prefix>/neon/DATABASE_URL`` Vault entry
    -- the same entry the connector and ``minds env deploy`` use. Per-env tiers
    (``dev`` / ``ci``) return None so the admin CLI auto-resolves the DSN from
    the per-env ``secrets.toml`` that ``minds env deploy`` wrote (this path never
    touches Vault).

    The wrapper resolves every per-tier secret the bake needs from the same Vault
    prefix, so the operator never hand-passes ``--database-url`` for staging /
    production.

    Raises ``click.ClickException`` if the Vault read fails or the entry lacks
    a non-empty ``DATABASE_URL``.
    """
    if explicit_database_url is not None:
        return explicit_database_url
    tier = tier_for_env_name(env_name)
    if tier not in (PRODUCTION_ENV_NAME, STAGING_ENV_NAME):
        return None
    deploy_config = load_deploy_config(tier)
    vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    try:
        secret = read_vault_kv(VaultPath(f"{vault_prefix}/neon"), parent_concurrency_group=parent_cg)
    except VaultReadError as exc:
        raise click.ClickException(
            f"Could not read the host_pool DSN from Vault ({vault_prefix}/neon) for env '{env_name}': {exc}"
        ) from exc
    dsn = secret.get(_POOL_DSN_VAULT_FIELD, "")
    if not dsn:
        raise click.ClickException(
            f"Vault entry {vault_prefix}/neon is missing {_POOL_DSN_VAULT_FIELD!r}; "
            "see apps/minds/docs/host-pool-setup.md step 3 for the schema."
        )
    return dsn


def run_imbue_cloud_admin_command(
    subgroup: str, args: list[str], *, extra_env: Mapping[str, str] | None
) -> FinishedProcess:
    """Run ``mngr imbue_cloud admin <subgroup> <args>`` and return the result.

    Streams the child's output line-by-line so a multi-host bake isn't a
    silent black box. Forwards the current process env, with ``extra_env``
    layered on top so callers can inject the activated tier's POOL_SSH_PRIVATE_KEY
    (read from Vault) without mutating the parent process's environment. Shared by
    the ``minds pool`` and ``minds server`` wrapper commands.
    """
    full_command = ["mngr", "imbue_cloud", "admin", subgroup] + args
    loggable_command = redact_secret_flag_values(full_command, secret_bearing_flags=_SECRET_BEARING_FLAGS)
    logger.info("Running: {}", " ".join(shlex.quote(part) for part in loggable_command))
    subprocess_env: dict[str, str] | None = None
    if extra_env:
        subprocess_env = merge_extra_env_into_subprocess_env(shell_env=os.environ, extra_env=extra_env)
    cg = ConcurrencyGroup(name=f"minds-{subgroup}")
    with cg:
        return cg.run_process_to_completion(
            command=full_command,
            timeout=float(_POOL_COMMAND_TIMEOUT_SECONDS),
            is_checked_after=False,
            on_output=_stream_subprocess_line,
            env=subprocess_env,
        )


def raise_on_admin_command_failure(subgroup: str, label: str, result: FinishedProcess) -> None:
    """Translate a non-zero admin subprocess exit into a ClickException naming the command."""
    if result.returncode != 0:
        raise click.ClickException(f"mngr imbue_cloud admin {subgroup} {label} failed (exit {result.returncode}).")


def _run_slice_pool_create(
    *,
    env_name: str,
    count: int,
    region: str,
    from_tag: str | None,
    repo_url: str | None,
    workspace_dir: str | None,
    repo_branch_or_tag_override: str | None,
    attributes_json: str | None,
    database_url: str | None,
    mngr_source: str | None,
    server_id: str | None,
    is_dry_run: bool,
    is_deferred_install_wait_skipped: bool,
    max_concurrency: int | None,
) -> None:
    """Resolve the pool private key from Vault, then bake bare-metal slice pool hosts.

    Slice baking targets the explicitly-chosen ``--server-id`` bare-metal box (see
    ``mngr imbue_cloud admin server list``) and authorizes the pool key from the
    tier's Vault entry at carve time.
    """
    if not server_id:
        raise click.UsageError(
            "--server-id is required (the bare-metal box to bake onto; see `mngr imbue_cloud admin server list`)"
        )
    try:
        pool_private_key = read_pool_private_key_from_vault(env_name)
    except VaultReadError as exc:
        raise click.ClickException(
            f"Could not read the pool SSH private key from Vault for env '{env_name}': {exc}"
        ) from exc
    args = build_create_admin_args(
        env_name=env_name,
        count=count,
        region=region,
        from_tag=from_tag,
        repo_url=repo_url,
        workspace_dir=workspace_dir,
        repo_branch_or_tag_override=repo_branch_or_tag_override,
        attributes_json=attributes_json,
        database_url=database_url,
        mngr_source=mngr_source,
        server_id=server_id,
        is_dry_run=is_dry_run,
        is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
        max_concurrency=max_concurrency,
    )
    raise_on_admin_command_failure(
        "pool",
        "create",
        run_imbue_cloud_admin_command("pool", args, extra_env={POOL_PRIVATE_KEY_ENV_VAR: pool_private_key}),
    )


@click.group()
def pool() -> None:
    """Pool-host orchestration for the currently activated minds env."""


@pool.command(name="create")
@click.option("--count", required=True, type=int, help="Number of pool hosts to create")
@click.option(
    "--region",
    required=True,
    type=str,
    help=(
        "Lease-region label stamped on every new row (e.g. ``US-EAST-VA``, ``US-WEST-OR``) -- what "
        "the connector region-matches at lease time. It is the lease-region label only (NOT the "
        "box's raw datacenter code)."
    ),
)
@click.option(
    "--from-tag",
    "from_tag",
    default=None,
    help="[production] Clone --repo-url at this tag and bake from it. Mutually exclusive with --workspace-dir.",
)
@click.option(
    "--repo-url",
    "repo_url",
    default=None,
    help="[--from-tag only] Canonical repo to clone the tag from (default: the FCT remote).",
)
@click.option(
    "--workspace-dir",
    required=False,
    default=None,
    type=click.Path(exists=True),
    help="[dev] Bake from this template repo working tree. Mutually exclusive with --from-tag.",
)
@click.option(
    "--repo-branch-or-tag",
    "repo_branch_or_tag_override",
    default=None,
    help="[--workspace-dir only] Override the stamped branch label (default: the folder's current branch).",
)
@click.option(
    "--attributes",
    "attributes_json",
    required=False,
    default=None,
    help=(
        'Optional non-identity lease-attributes JSON (e.g. \'{"cpus":2,"memory_gb":4}\'). repo_url and '
        "repo_branch_or_tag are derived from the bake source, not passed here."
    ),
)
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=DATABASE_URL_HELP,
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
        "`mngr imbue_cloud admin server list`). Slice baking targets an explicitly-chosen, ready box."
    ),
)
@click.option(
    "--dry-run",
    "is_dry_run",
    is_flag=True,
    default=False,
    help="Report the chosen server + per-slice sizing; do not bake.",
)
@click.option(
    "--max-concurrency",
    "max_concurrency",
    type=int,
    default=None,
    help=(
        "Max slices baked at once; the rest queue. Bounds box contention so each "
        "`mngr create` stays under its timeout. Omitted: the admin CLI's default applies."
    ),
)
@click.option(
    "--skip-deferred-install-wait",
    "is_deferred_install_wait_skipped",
    is_flag=True,
    default=False,
    help=(
        "[dev only] Don't wait for the FCT deferred-install (heavy apt + Playwright/Chromium) before "
        "stopping the baked services agent. Faster, but the baked container's deferred-install may be "
        "incomplete. Never use for production hosts."
    ),
)
def pool_create(
    count: int,
    region: str,
    from_tag: str | None,
    repo_url: str | None,
    workspace_dir: str | None,
    repo_branch_or_tag_override: str | None,
    attributes_json: str | None,
    database_url: str | None,
    mngr_source: str | None,
    server_id: str | None,
    is_dry_run: bool,
    max_concurrency: int | None,
    is_deferred_install_wait_skipped: bool,
) -> None:
    """Create bare-metal slice pool hosts for the activated minds env.

    Resolves the activated tier's POOL_SSH_PRIVATE_KEY from Vault (used to SSH the
    bare-metal box and carve the lima VM) so the operator never exports it by hand.
    The activated env dictates the tier, keeping "I'm on dev, I bake against the dev
    account using the dev keypair" the unambiguous default and making the
    keypair-mismatch class of bake failures unreachable for the standard path.
    """
    env_name = require_activated_env_name()
    effective_database_url = resolve_host_pool_dsn(env_name, database_url)
    _run_slice_pool_create(
        env_name=env_name,
        count=count,
        region=region,
        from_tag=from_tag,
        repo_url=repo_url,
        workspace_dir=workspace_dir,
        repo_branch_or_tag_override=repo_branch_or_tag_override,
        attributes_json=attributes_json,
        database_url=effective_database_url,
        mngr_source=mngr_source,
        server_id=server_id,
        is_dry_run=is_dry_run,
        is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
        max_concurrency=max_concurrency,
    )


@pool.command(name="list")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=DATABASE_URL_HELP,
)
def pool_list(database_url: str | None) -> None:
    """List pool_hosts rows (forwards to ``mngr imbue_cloud admin pool list``)."""
    # No env-name filter on the rows: the admin command does not know about
    # minds_env today and we don't want to start parsing its JSON output here
    # just to filter. Operators who only want rows for the active env can pipe
    # the JSON through ``jq``. The activated env name is still needed to resolve
    # the staging/production host_pool DSN from Vault.
    env_name = require_activated_env_name()
    args = build_list_admin_args(database_url=resolve_host_pool_dsn(env_name, database_url))
    raise_on_admin_command_failure("pool", "list", run_imbue_cloud_admin_command("pool", args, extra_env=None))


@pool.command(name="backfill-host-keys")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=DATABASE_URL_HELP,
)
def pool_backfill_host_keys(database_url: str | None) -> None:
    """One-time: keyscan + record SSH host public keys for pre-existing pool rows and boxes.

    Forwards to ``mngr imbue_cloud admin pool backfill-host-keys`` -- the single
    sanctioned trust-on-first-use, used once after deploying the host-key-pinning
    connector so rows baked before the host-key columns existed become leasable
    again. Resolves the staging / production host_pool DSN from the tier's
    ``<vault_prefix>/neon/DATABASE_URL`` Vault entry exactly like ``pool list`` /
    ``pool destroy``, so the operator never hand-passes ``--database-url``.
    Idempotent: rows that already have keys are skipped.
    """
    env_name = require_activated_env_name()
    args = build_backfill_host_keys_admin_args(database_url=resolve_host_pool_dsn(env_name, database_url))
    raise_on_admin_command_failure(
        "pool", "backfill-host-keys", run_imbue_cloud_admin_command("pool", args, extra_env=None)
    )


@pool.command(name="destroy")
@click.argument("pool_host_ids", nargs=-1, required=True)
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=DATABASE_URL_HELP,
)
@click.option(
    "--force",
    "is_leased_destroy_allowed",
    is_flag=True,
    help="Also destroy rows that are currently leased (tears down the leasing user's live workspace).",
)
@click.option(
    "--drop-row-only",
    "is_row_drop_only",
    is_flag=True,
    default=False,
    help=(
        "Only drop the DB rows; do NOT attempt VM teardown. Exclusively for rows whose "
        "bare-metal box record is gone or whose machine is permanently dead."
    ),
)
@click.option(
    "--max-concurrency",
    "max_concurrency",
    type=int,
    default=None,
    help="Max hosts destroyed at once; the rest queue. Omitted: the admin CLI's default applies.",
)
def pool_destroy(
    pool_host_ids: tuple[str, ...],
    database_url: str | None,
    is_leased_destroy_allowed: bool,
    is_row_drop_only: bool,
    max_concurrency: int | None,
) -> None:
    """Full teardown of pool hosts: destroy each slice lima VM in parallel, then drop its row.

    Forwards all ids to one ``mngr imbue_cloud admin pool destroy`` invocation, which
    atomically claims each row (so a user lease can never race the teardown), destroys
    the slice lima VMs concurrently (freeing the box slots), and deletes the rows. The
    POOL_SSH_PRIVATE_KEY teardown secret is read from the activated tier's Vault entry
    and injected into the subprocess, mirroring ``pool create`` (skipped for
    ``--drop-row-only``, which never SSHes a box).
    """
    env_name = require_activated_env_name()
    extra_env: dict[str, str] | None = None
    if not is_row_drop_only:
        try:
            pool_private_key = read_pool_private_key_from_vault(env_name)
        except VaultReadError as exc:
            raise click.ClickException(
                f"Could not read the pool SSH private key from Vault for env '{env_name}': {exc}"
            ) from exc
        extra_env = {POOL_PRIVATE_KEY_ENV_VAR: pool_private_key}
    args = build_destroy_admin_args(
        pool_host_ids=list(pool_host_ids),
        database_url=resolve_host_pool_dsn(env_name, database_url),
        is_leased_destroy_allowed=is_leased_destroy_allowed,
        is_row_drop_only=is_row_drop_only,
        max_concurrency=max_concurrency,
    )
    raise_on_admin_command_failure("pool", "destroy", run_imbue_cloud_admin_command("pool", args, extra_env=extra_env))
