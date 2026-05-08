"""``mngr latchkey`` CLI subcommands.

The plugin currently exposes a single subcommand: ``ensure-gateway``.
It is idempotent and shares its on-disk state (``latchkey_gateway.json``,
``latchkey_default_permissions.json``) with the in-process Python API so
the CLI and library agree on which gateway is the live one.
"""

import json
from pathlib import Path
from typing import Any

import click
from pydantic import Field

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.help_formatter import show_help_with_pager
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_latchkey.core import LATCHKEY_BINARY
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError


class EnsureGatewayCliOptions(CommonCliOptions):
    """Per-invocation options for ``mngr latchkey ensure-gateway``."""

    data_dir: str | None = Field(
        default=None,
        description=(
            "Directory in which to persist the gateway record and the deny-all "
            "default permissions file. Defaults to ``<profile>/latchkey``."
        ),
    )
    latchkey_directory: str | None = Field(
        default=None,
        description=(
            "Value to pass through as ``LATCHKEY_DIRECTORY`` to every spawned "
            "latchkey subprocess. Defaults to ``<data_dir>/latchkey-credentials``."
        ),
    )


def _resolve_data_dir(mngr_ctx: MngrContext, override: str | None) -> Path:
    """Pick the data dir for the gateway record and default permissions file.

    Defaults to ``<profile>/latchkey`` so the plugin's state lives next
    to the rest of mngr's per-profile data and survives invocations.
    """
    if override is not None:
        return Path(override).expanduser()
    return mngr_ctx.profile_dir / "latchkey"


def _resolve_latchkey_directory(data_dir: Path, override: str | None) -> Path:
    """Pick the ``LATCHKEY_DIRECTORY`` (credential / config store) for spawned subprocesses.

    Kept *separate* from ``data_dir`` (which holds gateway / permissions
    metadata owned by the plugin) so the user can point the credential
    store at a shared system location while keeping plugin metadata in
    the per-profile dir.
    """
    if override is not None:
        return Path(override).expanduser()
    return data_dir / "latchkey-credentials"


@click.group(name="latchkey", invoke_without_command=True)
@add_common_options
@click.pass_context
def latchkey(ctx: click.Context, **kwargs: Any) -> None:
    """Manage the shared latchkey gateway."""
    if ctx.invoked_subcommand is None:
        mngr_ctx, _, _ = setup_command_context(
            ctx=ctx,
            command_name="latchkey",
            command_class=CommonCliOptions,
        )
        show_help_with_pager(ctx, ctx.command, mngr_ctx.config)


@latchkey.command(name="ensure-gateway")
@click.option(
    "--data-dir",
    "data_dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=str),
    default=None,
    help="Directory for the gateway record + default permissions file (defaults to <profile>/latchkey).",
)
@click.option(
    "--latchkey-directory",
    "latchkey_directory",
    type=click.Path(file_okay=False, dir_okay=True, path_type=str),
    default=None,
    help="Value to pass as LATCHKEY_DIRECTORY to spawned latchkey subprocesses.",
)
@add_common_options
@click.pass_context
def ensure_gateway(ctx: click.Context, **kwargs: Any) -> None:
    """Start the shared latchkey gateway if it is not already running.

    Idempotent: the gateway record is adopted when a live one already
    exists, and a fresh subprocess is spawned otherwise. Prints the
    gateway info as a single JSON line to stdout on success.
    """
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="ensure-gateway",
        command_class=EnsureGatewayCliOptions,
    )
    data_dir = _resolve_data_dir(mngr_ctx, opts.data_dir)
    latchkey_directory = _resolve_latchkey_directory(data_dir, opts.latchkey_directory)

    instance = Latchkey(latchkey_binary=LATCHKEY_BINARY, latchkey_directory=latchkey_directory)
    instance.initialize(data_dir=data_dir)

    try:
        info = instance.ensure_gateway_started()
    except LatchkeyError as e:
        write_human_line("Failed to start latchkey gateway: {}", e)
        raise click.exceptions.Exit(code=1) from e

    payload = {
        "host": info.host,
        "port": info.port,
        "pid": info.pid,
        "started_at": info.started_at.isoformat(),
        "data_dir": str(data_dir),
        "latchkey_directory": str(latchkey_directory),
    }
    click.echo(json.dumps(payload))


CommandHelpMetadata(
    key="latchkey ensure-gateway",
    one_line_description="Start (or adopt) the shared latchkey gateway",
    synopsis="mngr latchkey ensure-gateway [--data-dir PATH] [--latchkey-directory PATH]",
    description="""Idempotently start the single shared ``latchkey gateway`` subprocess.

If a live gateway is already recorded under ``--data-dir``, this command
adopts it and exits successfully. Otherwise it spawns a fresh detached
gateway and persists its record so subsequent invocations (and the
in-process Python API in ``imbue.mngr_latchkey``) see the same gateway.

The gateway is detached: it survives this command's exit. Use it via
the agent-side ``LATCHKEY_GATEWAY`` env var that the per-agent setup
helpers in ``imbue.mngr_latchkey.agent_setup`` produce.

Output is a single JSON line on stdout describing the live gateway
(host, port, pid, started_at) so callers can pipe it into other tools.""",
    examples=(
        ("Ensure the gateway under the active profile", "mngr latchkey ensure-gateway"),
        (
            "Ensure the gateway under a custom data directory",
            "mngr latchkey ensure-gateway --data-dir ~/.minds",
        ),
    ),
).register()

add_pager_help_option(ensure_gateway)
