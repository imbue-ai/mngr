"""``minds pool {create,list,destroy}`` -- env-aware wrapper around ``mngr imbue_cloud admin pool``.

Responsibility split:

* ``mngr imbue_cloud admin pool create`` (in ``libs/mngr_imbue_cloud``) is the
  provider-generic host-creation step. It accepts a required ``--region`` and
  repeatable ``--tag KEY=VALUE`` and knows nothing about minds environments.
* This module is the env-aware layer: it requires an activated minds env
  (``MINDS_ROOT_NAME``), derives the env name, and injects
  ``--tag minds_env=<env-name>`` so ``minds env destroy`` can later enumerate
  + delete every VPS the env owns (via the OVH IAM v2 tag walker in
  :mod:`imbue.minds.envs.providers.ovh_tags`). All other admin flags
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

import shlex
import sys
from typing import Final

import click
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.minds.cli._activated_env import require_activated_env_name

_POOL_COMMAND_TIMEOUT_SECONDS: Final[int] = 7200


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


def _run_admin_command(args: list[str]) -> FinishedProcess:
    """Run ``mngr imbue_cloud admin pool <args>`` and return the result.

    Streams the child's output line-by-line so a multi-host bake isn't a
    silent black box. Forwards the current process env unchanged so the
    operator's OVH credentials / ``DATABASE_URL`` / ``MINDS_ROOT_NAME``
    reach the subprocess.
    """
    full_command = ["mngr", "imbue_cloud", "admin", "pool"] + args
    logger.info("Running: {}", " ".join(shlex.quote(part) for part in full_command))
    cg = ConcurrencyGroup(name="minds-pool")
    with cg:
        return cg.run_process_to_completion(
            command=full_command,
            timeout=float(_POOL_COMMAND_TIMEOUT_SECONDS),
            is_checked_after=False,
            on_output=_stream_subprocess_line,
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
    """Create pool hosts for the activated minds env (OVH-backed via admin)."""
    env_name = require_activated_env_name()
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
    _raise_on_failure("create", _run_admin_command(args))


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
