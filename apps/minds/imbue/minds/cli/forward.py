from pathlib import Path

import click
from loguru import logger

from imbue.minds.config.data_types import DEFAULT_FORWARDING_SERVER_HOST
from imbue.minds.config.data_types import DEFAULT_FORWARDING_SERVER_PORT
from imbue.minds.config.data_types import get_default_data_dir
from imbue.minds.forwarding_server.runner import start_forwarding_server


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
    help="Data directory for minds state (default: ~/.minds)",
)
def forward(host: str, port: int, data_dir: str | None) -> None:
    """Start the local forwarding server.

    The forwarding server handles authentication and proxies web traffic
    to individual mind web servers. It discovers backends by calling
    mngr CLI commands (mngr list, mngr events).
    """
    data_directory = Path(data_dir) if data_dir else get_default_data_dir()

    logger.info("Starting minds forwarding server...")
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
