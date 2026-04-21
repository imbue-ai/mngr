import os

import click
from loguru import logger

from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.config.data_types import DEFAULT_DESKTOP_CLIENT_HOST
from imbue.minds.config.data_types import DEFAULT_DESKTOP_CLIENT_PORT
from imbue.minds.desktop_client.minds_config import MindsConfig
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
    "--no-browser",
    is_flag=True,
    default=False,
    help="Do not open the login URL in the system browser",
)
@click.pass_context
def forward(ctx: click.Context, host: str, port: int, no_browser: bool) -> None:
    """Start the local desktop client.

    The desktop client handles authentication and proxies web traffic
    to individual workspace web servers. It discovers backends by calling
    mngr CLI commands (mngr list, mngr events).

    Data directory, mngr host directory, and mngr prefix are all derived
    from the MINDS_ROOT_NAME environment variable (default: "minds").
    """
    root_name = resolve_minds_root_name()
    data_directory = minds_data_dir_for(root_name)
    minds_config = MindsConfig(data_dir=data_directory)
    output_format: OutputFormat = ctx.obj.get("output_format", OutputFormat.HUMAN)

    logger.info("Starting minds desktop client...")
    logger.info("  Listening on: http://{}:{}", host, port)
    logger.info("  MINDS_ROOT_NAME: {}", root_name)
    logger.info("  Data directory: {}", data_directory)
    logger.info("  MNGR_HOST_DIR: {}", os.environ.get("MNGR_HOST_DIR", "<unset>"))
    logger.info("  MNGR_PREFIX: {}", os.environ.get("MNGR_PREFIX", "<unset>"))
    logger.info("  cloudflare_forwarding_url: {}", minds_config.cloudflare_forwarding_url)
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
