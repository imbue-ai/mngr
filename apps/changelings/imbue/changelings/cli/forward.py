from pathlib import Path

import click
from loguru import logger

from imbue.changelings.config.data_types import DEFAULT_FORWARDING_SERVER_HOST
from imbue.changelings.config.data_types import DEFAULT_FORWARDING_SERVER_PORT
from imbue.changelings.config.data_types import get_default_data_dir
from imbue.changelings.forwarding_server.runner import start_forwarding_server


@click.command()
@click.option(
    "--host",
    default=DEFAULT_FORWARDING_SERVER_HOST,
    show_default=True,
    help="Host to bind the forwarding server to",
)
@click.option(
    "--port",
    default=DEFAULT_FORWARDING_SERVER_PORT,
    show_default=True,
    help="Port to bind the forwarding server to",
)
@click.option(
    "--data-dir",
    type=click.Path(resolve_path=True),
    default=None,
    help="Data directory for changelings state (default: ~/.changelings)",
)
def forward(host: str, port: int, data_dir: str | None) -> None:
    """Start the local forwarding server.

    The forwarding server handles authentication and proxies web traffic
    to individual changeling web servers. It discovers backends by calling
    mng CLI commands (mng list, mng logs).
    """
    data_directory = Path(data_dir) if data_dir else get_default_data_dir()

    logger.info("Starting changelings forwarding server...")
    logger.info("  Listening on: http://{}:{}", host, port)
    logger.info("  Data directory: {}", data_directory)
    logger.info("")
    logger.info("Press Ctrl+C to stop.")
    logger.info("")

    start_forwarding_server(
        data_directory=data_directory,
        host=host,
        port=port,
    )
