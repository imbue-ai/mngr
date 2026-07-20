"""Click entry point for ``mngr foreman`` -- the always-on web remote-control server."""

from typing import Any

import click
from loguru import logger

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.utils.parent_process import start_parent_death_watcher
from imbue.mngr_foreman.config import ForemanPluginConfig
from imbue.mngr_foreman.server import run_server


class ForemanCliOptions(CommonCliOptions):
    """Options for ``mngr foreman``. Backed by the click flags below."""

    host: str | None = None
    port: int | None = None


def _resolve_foreman_config(mngr_ctx: Any) -> ForemanPluginConfig:
    """Pull the merged ``[plugins.foreman]`` config, falling back to defaults."""
    plugins = getattr(mngr_ctx.config, "plugins", {}) or {}
    raw = plugins.get("foreman")
    if isinstance(raw, ForemanPluginConfig):
        return raw
    if isinstance(raw, dict):
        return ForemanPluginConfig.model_validate(raw)
    return ForemanPluginConfig()


@click.command(name="foreman")
@click.option("--host", default=None, help="Bind host (default from config, else 0.0.0.0).")
@click.option("--port", type=int, default=None, help="Bind port (default from config, else 8700).")
@add_common_options
@click.pass_context
def foreman(ctx: click.Context, **kwargs: Any) -> None:
    """Always-on web remote control for your mngr agents [experimental].

    Runs a single Flask server on this box, over every agent in mngr's view:
    a chat UI for claude agents (live transcript + send) and a web terminal for
    any agent type, drivable from any device (including a phone). No code is
    deployed to target boxes and there is no auth -- bind to a tailnet IP or
    firewall the port. Create agents with plain ``mngr create``.

    \b
    Examples:
      mngr foreman --port 8700
      mngr foreman --host 100.64.0.1
    """
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="foreman",
        command_class=ForemanCliOptions,
        is_format_template_supported=False,
    )

    start_parent_death_watcher(mngr_ctx.concurrency_group)

    config = _resolve_foreman_config(mngr_ctx)
    host = opts.host if opts.host is not None else config.host
    port = opts.port if opts.port is not None else config.port

    logger.info(
        "Starting foreman server on http://{}:{} (max_tool_output_chars={})",
        host,
        port,
        config.max_tool_output_chars,
    )

    run_server(
        mngr_ctx=mngr_ctx,
        host=host,
        port=port,
        max_tool_output_chars=config.max_tool_output_chars,
    )


CommandHelpMetadata(
    key="foreman",
    one_line_description="Always-on web remote control for your mngr agents [experimental]",
    synopsis="mngr foreman [--host HOST] [--port PORT] [OPTIONS]",
    description="""Runs a single Flask server on this box, over every agent in
mngr's view: a mobile-friendly chat UI for claude agents (live transcript with
markdown, syntax highlighting, KaTeX, mermaid, inline images and file uploads;
send messages; interrupt) and a web terminal (xterm.js over a pty bridge) for
any agent type.

No code is deployed to target boxes and there is no auth by design -- bind to a
tailnet IP or firewall the port. Create agents with plain ``mngr create``;
there is no foreman-specific create command or label filter.""",
    examples=(
        ("Serve on the default port", "mngr foreman --port 8700"),
        ("Bind to a specific tailnet IP", "mngr foreman --host 100.64.0.1"),
    ),
).register()

add_pager_help_option(foreman)
