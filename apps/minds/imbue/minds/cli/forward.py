from pathlib import Path

import click
from loguru import logger

from imbue.minds.config.data_types import DEFAULT_FORWARDING_SERVER_HOST
from imbue.minds.config.data_types import DEFAULT_FORWARDING_SERVER_PORT
from imbue.minds.config.data_types import get_default_data_dir
from imbue.minds.forwarding_server.runner import start_forwarding_server
from imbue.minds.utils.logging import LogFormat


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
@click.pass_context
def forward(ctx: click.Context, host: str, port: int, data_dir: str | None) -> None:
    """Start the local forwarding server.

    The forwarding server handles authentication and proxies web traffic
    to individual mind web servers. It discovers backends by calling
    mngr CLI commands (mngr list, mngr events).
    """
    data_directory = Path(data_dir) if data_dir else get_default_data_dir()

    # JSONL log format implies headless mode (no browser open, no duplicate
    # human-readable URL output) because the Electron shell is the consumer.
    log_format: LogFormat = ctx.obj.get("log_format", LogFormat.TEXT)
    is_headless = log_format == LogFormat.JSONL

    if not is_headless:
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
        is_headless=is_headless,
    )
