from pathlib import Path
from typing import Any
from typing import assert_never

import click
from loguru import logger

from imbue.concurrency_group.errors import ProcessError
from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import AbortError
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat
from imbue.mng.uv_tool import get_receipt_path


def _require_uv_tool_for_self_upgrade(receipt_path: Path | None) -> None:
    """Raise AbortError if mng was not installed via ``uv tool``.

    Uses the same detection mechanism as ``require_uv_tool_receipt`` but
    provides an error message specific to self-upgrade (rather than plugin
    management).
    """
    if receipt_path is None:
        raise AbortError(
            "The current mng instance is not installed via 'uv tool install'. "
            "To upgrade mng, use whatever commands you use to manage Python dependencies."
        )


class SelfUpgradeCliOptions(CommonCliOptions):
    """Options passed from the CLI to the self-upgrade command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.
    """


@click.command(name="selfupgrade")
@add_common_options
@click.pass_context
def self_upgrade(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _self_upgrade_impl(ctx)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _self_upgrade_impl(ctx: click.Context) -> None:
    """Implementation of self-upgrade command."""
    mng_ctx, output_opts, _opts = setup_command_context(
        ctx=ctx,
        command_name="selfupgrade",
        command_class=SelfUpgradeCliOptions,
    )

    _require_uv_tool_for_self_upgrade(get_receipt_path())

    stdout = _run_uv_tool_upgrade(mng_ctx.concurrency_group)
    _emit_self_upgrade_result(stdout, output_opts)


def _run_uv_tool_upgrade(concurrency_group: Any) -> str:
    """Run ``uv tool upgrade mng`` and return the stripped stdout.

    Raises AbortError if the process fails.
    """
    command = ("uv", "tool", "upgrade", "mng")
    try:
        result = concurrency_group.run_process_to_completion(command)
    except ProcessError as e:
        raise AbortError(
            f"Failed to upgrade mng: {e.stderr.strip() or e.stdout.strip()}",
            original_exception=e,
        ) from e
    return result.stdout.strip()


def _emit_self_upgrade_result(
    stdout: str,
    output_opts: OutputOptions,
) -> None:
    """Emit the result of a self-upgrade operation."""
    match output_opts.output_format:
        case OutputFormat.HUMAN:
            if stdout:
                write_human_line("{}", stdout)
            else:
                write_human_line("mng upgraded successfully.")
        case OutputFormat.JSON:
            emit_final_json({"upgraded": True, "message": stdout})
        case OutputFormat.JSONL:
            emit_final_json({"event": "self_upgraded", "message": stdout})
        case _ as unreachable:
            assert_never(unreachable)


CommandHelpMetadata(
    key="selfupgrade",
    one_line_description="Upgrade mng to the latest version",
    synopsis="mng selfupgrade [OPTIONS]",
    description="""Upgrades the mng tool to the latest available version using ``uv tool upgrade``.

This command requires that mng was installed via ``uv tool install``. If mng
was installed through other means, use your package manager's upgrade command
instead.""",
    examples=(
        ("Upgrade mng", "mng selfupgrade"),
        ("Upgrade with JSON output", "mng selfupgrade --format json"),
    ),
    see_also=(("plugin", "Manage available and active plugins"),),
).register()

add_pager_help_option(self_upgrade)
