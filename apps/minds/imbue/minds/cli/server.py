"""``minds server {list,prep}`` -- env-aware wrapper around ``mngr imbue_cloud admin server``.

Mirrors the ``minds pool`` wrapper (see :mod:`imbue.minds.cli.pool`): from the
activated minds env it resolves the host_pool DSN (Vault for staging/production,
per-env secrets.toml for dev/ci) and, for ``prep``, injects the tier's
POOL_SSH_PRIVATE_KEY from Vault into the admin subprocess -- so operators never
hand-export MINDS_HOST_POOL_DSN or the pool key to inspect or (re-)prep boxes.
The remaining ``admin server`` subcommands (order / register / setup / ...) also
need OVH supplier credentials and stay unwrapped.

The argument-construction logic (``build_*_args``) is split out from the click
commands so unit tests can verify the flag forwarding without a fake subprocess.
"""

import click
from loguru import logger

from imbue.minds.cli._activated_env import require_activated_env_name
from imbue.minds.cli.pool import DATABASE_URL_HELP
from imbue.minds.cli.pool import POOL_PRIVATE_KEY_ENV_VAR
from imbue.minds.cli.pool import raise_on_admin_command_failure
from imbue.minds.cli.pool import read_pool_private_key_from_vault
from imbue.minds.cli.pool import resolve_host_pool_dsn
from imbue.minds.cli.pool import run_imbue_cloud_admin_command
from imbue.minds.envs.primitives import VaultReadError


def build_server_list_admin_args(*, database_url: str | None) -> list[str]:
    """Compose the ``mngr imbue_cloud admin server list`` argv.

    ``--database-url`` forwarded only when non-None (dev auto-resolves it from the
    activated env's secrets.toml; staging/production pass the Vault-resolved DSN).
    """
    args = ["list"]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    return args


def build_server_prep_admin_args(
    *,
    server_id: str,
    database_url: str | None,
    lima_version: str | None,
    slice_base_image_url: str | None,
) -> list[str]:
    """Compose the ``mngr imbue_cloud admin server prep`` argv.

    The optional overrides are forwarded only when set, so the admin CLI's own
    defaults (current lima release, default guest image) stay the single source
    of truth.
    """
    args = ["prep", "--server-id", server_id]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    if lima_version is not None:
        args.extend(["--lima-version", lima_version])
    if slice_base_image_url is not None:
        args.extend(["--slice-base-image-url", slice_base_image_url])
    return args


@click.group()
def server() -> None:
    """Bare-metal server operations for the currently activated minds env."""


@server.command(name="list")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=DATABASE_URL_HELP,
)
def server_list(database_url: str | None) -> None:
    """List bare-metal servers with per-server and fleet slot accounting.

    Forwards to ``mngr imbue_cloud admin server list``, resolving the host_pool
    DSN from the activated env exactly like ``minds pool list``.
    """
    env_name = require_activated_env_name()
    args = build_server_list_admin_args(database_url=resolve_host_pool_dsn(env_name, database_url))
    raise_on_admin_command_failure("server", "list", run_imbue_cloud_admin_command("server", args, extra_env=None))


@server.command(name="prep")
@click.option(
    "--server-id",
    "server_id",
    required=True,
    help="bare_metal_servers row id of the box to (re-)prep (from `minds server list`).",
)
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=DATABASE_URL_HELP,
)
@click.option(
    "--lima-version",
    "lima_version",
    default=None,
    help="Override the lima release to install on the box (default: the admin CLI's pinned version).",
)
@click.option(
    "--slice-base-image-url",
    "slice_base_image_url",
    default=None,
    help="Override the guest OS image staged on the box (default: the admin CLI's pinned image).",
)
def server_prep(
    server_id: str,
    database_url: str | None,
    lima_version: str | None,
    slice_base_image_url: str | None,
) -> None:
    """(Re-)prep a bare-metal box for slice baking (qemu/lima/tooling + image staging).

    Forwards to ``mngr imbue_cloud admin server prep``, resolving the host_pool DSN
    from the activated env and injecting the tier's POOL_SSH_PRIVATE_KEY from Vault
    (the prep authorizes that key on the box), mirroring ``minds pool create``.
    Idempotent -- also the way to bring a box prepped before 2026-06-27 up to date
    (older preps lack the per-box DEFAULT_WORKSPACE_TEMPLATE image cache dir that production ``--from-tag``
    bakes require).
    """
    env_name = require_activated_env_name()
    try:
        pool_private_key = read_pool_private_key_from_vault(env_name)
    except VaultReadError as exc:
        raise click.ClickException(
            f"Could not read the pool SSH private key from Vault for env '{env_name}': {exc}"
        ) from exc
    args = build_server_prep_admin_args(
        server_id=server_id,
        database_url=resolve_host_pool_dsn(env_name, database_url),
        lima_version=lima_version,
        slice_base_image_url=slice_base_image_url,
    )
    logger.info("Prepping bare-metal server {} for env '{}'", server_id, env_name)
    raise_on_admin_command_failure(
        "server",
        "prep",
        run_imbue_cloud_admin_command("server", args, extra_env={POOL_PRIVATE_KEY_ENV_VAR: pool_private_key}),
    )
