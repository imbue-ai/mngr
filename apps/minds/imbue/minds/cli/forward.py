from pathlib import Path

import click
from loguru import logger

from imbue.minds.config.data_types import DEFAULT_DESKTOP_CLIENT_HOST
from imbue.minds.config.data_types import DEFAULT_DESKTOP_CLIENT_PORT
from imbue.minds.config.data_types import get_default_data_dir
from imbue.minds.desktop_client.runner import start_desktop_client
from imbue.minds.primitives import OutputFormat


@click.command()
@click.option(
    "--host",
    default=DEFAULT_DESKTOP_CLIENT_HOST,
    show_default=True,
    help="Host to bind the desktop client to",
)
@click.option(
    "--port",
    default=DEFAULT_DESKTOP_CLIENT_PORT,
    show_default=True,
    help="Port to bind the desktop client to",
)
@click.option(
    "--data-dir",
    type=click.Path(resolve_path=True),
    default=None,
    help="Data directory for minds state (default: ~/.minds)",
)
@click.option(
    "--no-browser",
    is_flag=True,
    default=False,
    help="Do not open the login URL in the system browser",
)
@click.pass_context
def forward(ctx: click.Context, host: str, port: int, data_dir: str | None, no_browser: bool) -> None:
    """Start the local desktop client.

    The desktop client handles authentication and proxies web traffic
    to individual mind web servers. It discovers backends by calling
    mngr CLI commands (mngr list, mngr events).
    """
    data_directory = Path(data_dir) if data_dir else get_default_data_dir()
    output_format: OutputFormat = ctx.obj.get("output_format", OutputFormat.HUMAN)

    logger.info("Starting minds desktop client...")
    logger.info("  Listening on: http://{}:{}", host, port)
    logger.info("  Data directory: {}", data_directory)
    logger.info("")
    logger.info("Press Ctrl+C to stop.")
    logger.info("")

    start_desktop_client(
        data_directory=data_directory,
        host=host,
        port=port,
        output_format=output_format,
        is_no_browser=no_browser,
    )
