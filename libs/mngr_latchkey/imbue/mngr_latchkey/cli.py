"""``mngr latchkey`` CLI subcommands.

The plugin currently exposes a single subcommand: ``ensure-gateway``.
It is idempotent and shares its on-disk state (``latchkey_gateway.json``,
``latchkey_default_permissions.json``) with the in-process Python API so
the CLI and library agree on which gateway is the live one.
"""

import json
from pathlib import Path
from typing import Any
from typing import Final

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

# Sub-directory name used inside the active profile when no ``--latchkey-dir``
# is supplied. Centralized as a constant so the click option help text, the
# pydantic field description, and ``_resolve_latchkey_dir`` cannot drift
# apart.
DEFAULT_LATCHKEY_DIR_NAME: Final[str] = "latchkey"
_DEFAULT_LATCHKEY_DIR_DISPLAY: Final[str] = f"<profile>/{DEFAULT_LATCHKEY_DIR_NAME}"


class EnsureGatewayCliOptions(CommonCliOptions):
    """Per-invocation options for ``mngr latchkey ensure-gateway``."""

    latchkey_dir: str | None = Field(
        default=None,
        description=(
            f"Directory for the gateway record, the deny-all default permissions file, "
            f"per-agent permissions handles, and the upstream ``LATCHKEY_DIRECTORY`` "
            f"credential / config store. Defaults to ``{_DEFAULT_LATCHKEY_DIR_DISPLAY}``."
        ),
    )


def _resolve_latchkey_dir(mngr_ctx: MngrContext, override: str | None) -> Path:
    """Pick the directory the plugin should use for both metadata and credentials.

    A single directory holds:

    * The plugin's own metadata (``latchkey_gateway.json``,
      ``latchkey_default_permissions.json``, the
      ``latchkey/permissions/`` subtree, ``agents/<agent_id>/`` subtree).
    * Whatever the upstream ``latchkey`` CLI itself decides to write
      under ``LATCHKEY_DIRECTORY``.

    These two sets of files don't collide today (different filename
    spaces) and keeping them together means the user has just one path
    to remember / move / back up.
    """
    if override is not None:
        return Path(override).expanduser()
    return mngr_ctx.profile_dir / DEFAULT_LATCHKEY_DIR_NAME


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
    "--latchkey-dir",
    "latchkey_dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=str),
    default=None,
    help=(
        "Directory for the gateway record, default permissions file, and "
        f"upstream LATCHKEY_DIRECTORY (defaults to {_DEFAULT_LATCHKEY_DIR_DISPLAY})."
    ),
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
    latchkey_dir = _resolve_latchkey_dir(mngr_ctx, opts.latchkey_dir)

    instance = Latchkey(latchkey_binary=LATCHKEY_BINARY, latchkey_directory=latchkey_dir)
    instance.initialize(data_dir=latchkey_dir)

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
        "latchkey_dir": str(latchkey_dir),
    }
    # Output the gateway info as a single JSON line so callers can pipe
    # it directly into ``jq``. ``write_human_line`` is the project's
    # standard way to write to stdout from a CLI command; the JSON
    # serialization happens to also be human-readable.
    write_human_line(json.dumps(payload))


CommandHelpMetadata(
    key="latchkey ensure-gateway",
    one_line_description="Start (or adopt) the shared latchkey gateway",
    synopsis="mngr latchkey ensure-gateway [--latchkey-dir PATH]",
    description="""Idempotently start the single shared ``latchkey gateway`` subprocess.

If a live gateway is already recorded under ``--latchkey-dir``, this command
adopts it and exits successfully. Otherwise it spawns a fresh detached
gateway and persists its record so subsequent invocations (and the
in-process Python API in ``imbue.mngr_latchkey``) see the same gateway.

The gateway is detached: it survives this command's exit. Use it via
the agent-side ``LATCHKEY_GATEWAY`` env var that the per-agent setup
helpers in ``imbue.mngr_latchkey.agent_setup`` produce.

The same directory is also used as ``LATCHKEY_DIRECTORY`` for spawned
latchkey subprocesses, so credentials and config live alongside the
plugin's own metadata.

Output is a single JSON line on stdout describing the live gateway
(host, port, pid, started_at) so callers can pipe it into other tools.""",
    examples=(
        ("Ensure the gateway under the active profile", "mngr latchkey ensure-gateway"),
        (
            "Ensure the gateway under a custom directory",
            "mngr latchkey ensure-gateway --latchkey-dir ~/.minds",
        ),
    ),
).register()

add_pager_help_option(ensure_gateway)
