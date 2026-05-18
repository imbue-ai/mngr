"""``minds pool {create,list,destroy}`` -- env-aware wrapper around ``mngr imbue_cloud admin pool``.

Responsibility split:

* ``mngr imbue_cloud admin pool create`` (in ``libs/mngr_imbue_cloud``) is the
  provider-generic host-creation step. It accepts a required ``--region`` and
  repeatable ``--tag KEY=VALUE`` and knows nothing about minds environments.
* This module is the env-aware layer: it requires an activated minds env
  (``MINDS_ROOT_NAME``), derives the env name, injects
  ``--tag minds_env=<env-name>`` so ``minds env destroy`` can later enumerate
  + delete every VPS the env owns (via the OVH IAM v2 tag walker in
  :mod:`imbue.minds.envs.providers.ovh_tags`), AND reads the activated
  tier's OVH AK/AS/CK from Vault (``<vault_path_prefix>/ovh``) and
  injects them into the subprocess env so the inner ``mngr create
  ... --template ovh`` actually has credentials. All other admin flags
  (``--count`` / ``--attributes`` / ``--workspace-dir`` /
  ``--management-public-key-file`` / ``--database-url`` / ``--mngr-source``)
  forward 1:1.

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
from typing import Final

import click
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.minds.cli._activated_env import require_activated_env_name
from imbue.minds.cli._activated_env import tier_for_env_name
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv

_POOL_COMMAND_TIMEOUT_SECONDS: Final[int] = 7200

# OVH provider-config env vars consumed by ``OvhProviderConfig`` (in
# ``libs/mngr_ovh``). The three AK/AS/CK keys are required; the
# endpoint is optional (defaults to ``ovh-us`` in the provider config).
_OVH_REQUIRED_ENV_VARS: Final[tuple[str, ...]] = (
    "OVH_APPLICATION_KEY",
    "OVH_APPLICATION_SECRET",
    "OVH_CONSUMER_KEY",
)
_OVH_OPTIONAL_ENV_VARS: Final[tuple[str, ...]] = ("OVH_ENDPOINT",)


def build_create_admin_args(
    *,
    env_name: str,
    count: int,
    region: str,
    attributes_json: str,
    workspace_dir: str,
    management_public_key_file: str,
    database_url: str | None,
    mngr_source: str | None,
) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool create`` argv from minds-side inputs.

    Auto-injects ``--tag minds_env=<env_name>``; forwards every other
    user-supplied flag verbatim. Split out from the click command so
    tests can exercise the wiring without faking a subprocess.

    ``--database-url`` is forwarded only when explicitly supplied. When
    omitted, the admin CLI auto-resolves the DSN from the activated
    minds env's ``secrets.toml`` (which the deploy wrote).
    """
    args = [
        "create",
        "--count",
        str(count),
        "--region",
        region,
        "--tag",
        f"minds_env={env_name}",
        "--attributes",
        attributes_json,
        "--workspace-dir",
        workspace_dir,
        "--management-public-key-file",
        management_public_key_file,
    ]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    if mngr_source is not None:
        args.extend(["--mngr-source", mngr_source])
    return args


def build_list_admin_args(*, database_url: str | None) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool list`` argv.

    ``--database-url`` forwarded only when explicitly supplied; see
    :func:`build_create_admin_args`.
    """
    args = ["list"]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    return args


def build_destroy_admin_args(*, pool_host_id: str, database_url: str | None, force: bool) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool destroy`` argv."""
    args = ["destroy", pool_host_id]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    if force:
        args.append("--force")
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


def merge_ovh_env_into_subprocess_env(*, shell_env: Mapping[str, str], ovh_env: Mapping[str, str]) -> dict[str, str]:
    """Build the subprocess env: start from ``shell_env``, then layer ``ovh_env`` on top.

    OVH values from the activated tier's Vault entry win over whatever the
    operator may have lying around in their shell. The operator's mental
    model when running ``minds pool create`` (with an activated env) is
    "this provisions hosts for the active tier" -- so the active tier's
    creds are the source of truth, not a stale ``OVH_APPLICATION_KEY``
    that might still be exported from a different tier's bake last week.

    Pure function so the precedence rule is testable without a fake
    subprocess runner or a fake Vault.
    """
    merged = dict(shell_env)
    merged.update(ovh_env)
    return merged


def resolve_ovh_env_from_vault(
    env_name: str,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> dict[str, str]:
    """Read the activated tier's OVH AK/AS/CK from Vault, return as env-var dict.

    Looks up the tier for ``env_name`` (``production`` / ``staging`` /
    ``dev``), loads the corresponding deploy config to discover
    ``vault_path_prefix``, then reads ``<prefix>/ovh`` from Vault via the
    standard ``read_vault_kv`` shellout (so the operator's existing
    ``vault login`` + ``VAULT_ADDR`` / ``VAULT_NAMESPACE`` are honored).

    The required keys ``OVH_APPLICATION_KEY`` / ``OVH_APPLICATION_SECRET``
    / ``OVH_CONSUMER_KEY`` must all be present and non-empty; the
    optional ``OVH_ENDPOINT`` is included if set. Missing required keys
    raise ``click.ClickException`` with a pointer at the setup doc.

    Raises ``VaultReadError`` if the Vault read itself fails (binary
    missing, not logged in, entry absent, malformed payload).
    """
    tier = tier_for_env_name(env_name)
    deploy_config = load_deploy_config(tier)
    vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    secret = read_vault_kv(VaultPath(f"{vault_prefix}/ovh"), parent_concurrency_group=parent_cg)
    missing = [key for key in _OVH_REQUIRED_ENV_VARS if not secret.get(key)]
    if missing:
        raise click.ClickException(
            f"Vault entry {vault_prefix}/ovh is missing required key(s) {missing}; "
            "see apps/minds/docs/host-pool-setup.md step 3 for the schema."
        )
    env_vars: dict[str, str] = {key: secret[key] for key in _OVH_REQUIRED_ENV_VARS}
    for key in _OVH_OPTIONAL_ENV_VARS:
        if value := secret.get(key):
            env_vars[key] = value
    return env_vars


def _run_admin_command(args: list[str], *, extra_env: Mapping[str, str] | None = None) -> FinishedProcess:
    """Run ``mngr imbue_cloud admin pool <args>`` and return the result.

    Streams the child's output line-by-line so a multi-host bake isn't a
    silent black box. Forwards the current process env, with ``extra_env``
    layered on top so callers can inject the activated tier's OVH AK/AS/CK
    (read from Vault by :func:`resolve_ovh_env_from_vault`) without having
    to mutate the parent process's environment.
    """
    full_command = ["mngr", "imbue_cloud", "admin", "pool"] + args
    logger.info("Running: {}", " ".join(shlex.quote(part) for part in full_command))
    subprocess_env: dict[str, str] | None = None
    if extra_env:
        subprocess_env = merge_ovh_env_into_subprocess_env(shell_env=os.environ, ovh_env=extra_env)
    cg = ConcurrencyGroup(name="minds-pool")
    with cg:
        return cg.run_process_to_completion(
            command=full_command,
            timeout=float(_POOL_COMMAND_TIMEOUT_SECONDS),
            is_checked_after=False,
            on_output=_stream_subprocess_line,
            env=subprocess_env,
        )


def _raise_on_failure(label: str, result: FinishedProcess) -> None:
    if result.returncode != 0:
        raise click.ClickException(f"mngr imbue_cloud admin pool {label} failed (exit {result.returncode}).")


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
        "OVH datacenter code for the new pool VPSes (e.g. ``US-EAST-VA``, ``US-WEST-OR``). "
        "Validated by OVH at order time."
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
        "Neon PostgreSQL direct connection string for the pool DB. Optional: "
        "defaults to the activated minds env's NEON_HOST_POOL_DSN (written by "
        "`minds env deploy`). Pass explicitly only when overriding."
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
    attributes_json: str,
    workspace_dir: str,
    management_public_key_file: str,
    database_url: str | None,
    mngr_source: str | None,
) -> None:
    """Create pool hosts for the activated minds env (OVH-backed via admin).

    Reads the activated tier's OVH AK/AS/CK from Vault before invoking the
    admin subcommand and injects them into the subprocess env, so the
    operator never has to manually export them. The activated env dictates
    the tier (and therefore the Vault path), which keeps "I'm on dev, I
    bake against the dev OVH account" the unambiguous default.
    """
    env_name = require_activated_env_name()
    try:
        ovh_env = resolve_ovh_env_from_vault(env_name)
    except VaultReadError as exc:
        raise click.ClickException(f"Could not read OVH credentials from Vault for env '{env_name}': {exc}") from exc
    args = build_create_admin_args(
        env_name=env_name,
        count=count,
        region=region,
        attributes_json=attributes_json,
        workspace_dir=workspace_dir,
        management_public_key_file=management_public_key_file,
        database_url=database_url,
        mngr_source=mngr_source,
    )
    _raise_on_failure("create", _run_admin_command(args, extra_env=ovh_env))


@pool.command(name="list")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=(
        "Neon PostgreSQL direct connection string for the pool DB. Optional: "
        "defaults to the activated minds env's NEON_HOST_POOL_DSN (written by "
        "`minds env deploy`). Pass explicitly only when overriding."
    ),
)
def pool_list(database_url: str | None) -> None:
    """List pool_hosts rows (forwards to ``mngr imbue_cloud admin pool list``)."""
    # No env-name filter: the admin command does not know about minds_env
    # today and we don't want to start parsing its JSON output here just to
    # filter. Operators who only want rows for the active env can pipe the
    # JSON through ``jq``. ``require_activated_env_name`` is still called
    # for consistency -- a pool list run outside an activated env is almost
    # always an operator mistake.
    require_activated_env_name()
    args = build_list_admin_args(database_url=database_url)
    _raise_on_failure("list", _run_admin_command(args))


@pool.command(name="destroy")
@click.argument("pool_host_id")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=(
        "Neon PostgreSQL direct connection string for the pool DB. Optional: "
        "defaults to the activated minds env's NEON_HOST_POOL_DSN (written by "
        "`minds env deploy`). Pass explicitly only when overriding."
    ),
)
@click.option("--force", is_flag=True, help="Drop the row even if status != 'released'")
def pool_destroy(pool_host_id: str, database_url: str | None, force: bool) -> None:
    """Remove a pool_hosts row by id (forwards to ``mngr imbue_cloud admin pool destroy``)."""
    require_activated_env_name()
    args = build_destroy_admin_args(pool_host_id=pool_host_id, database_url=database_url, force=force)
    _raise_on_failure("destroy", _run_admin_command(args))
