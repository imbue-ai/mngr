"""``mngr foreman serve`` -- run the always-on web remote-control server."""

from typing import Any

import click
from loguru import logger

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.utils.parent_process import start_parent_death_watcher
from imbue.mngr_foreman.config import ForemanPluginConfig
from imbue.mngr_foreman.server import run_server


class ForemanServeCliOptions(CommonCliOptions):
    """Options for ``mngr foreman serve``. Backed by the click flags below."""

    host: str | None = None
    port: int | None = None
    foreman_only: bool = False


def _resolve_foreman_config(mngr_ctx: Any) -> ForemanPluginConfig:
    """Pull the merged ``[plugins.foreman]`` config, falling back to defaults."""
    plugins = getattr(mngr_ctx.config, "plugins", {}) or {}
    raw = plugins.get("foreman")
    if isinstance(raw, ForemanPluginConfig):
        return raw
    if isinstance(raw, dict):
        return ForemanPluginConfig.model_validate(raw)
    return ForemanPluginConfig()


@click.command(name="serve")
@click.option("--host", default=None, help="Bind host (default from config, else 0.0.0.0).")
@click.option("--port", type=int, default=None, help="Bind port (default from config, else 8700).")
@click.option(
    "--foreman-only",
    is_flag=True,
    default=False,
    help="Only show agents labelled foreman=1 (created via `mngr foreman create` in phase 3).",
)
@add_common_options
@click.pass_context
def serve(ctx: click.Context, **kwargs: Any) -> None:
    """Serve the foreman web UI (agent list + chat + send)."""
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="serve",
        command_class=ForemanServeCliOptions,
        is_format_template_supported=False,
    )

    start_parent_death_watcher(mngr_ctx.concurrency_group)

    config = _resolve_foreman_config(mngr_ctx)
    host = opts.host if opts.host is not None else config.host
    port = opts.port if opts.port is not None else config.port
    foreman_only = opts.foreman_only or config.foreman_only

    logger.info(
        "Starting foreman server on http://{}:{} (foreman_only={}, max_tool_output_chars={})",
        host,
        port,
        foreman_only,
        config.max_tool_output_chars,
    )

    run_server(
        mngr_ctx=mngr_ctx,
        host=host,
        port=port,
        foreman_only=foreman_only,
        max_tool_output_chars=config.max_tool_output_chars,
    )
