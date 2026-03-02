from collections.abc import Callable

from loguru import logger

from imbue.mng.errors import MngError
from imbue.mng.utils.polling import run_periodically


def _run_iteration_with_logging(
    iteration_fn: Callable[[], None],
    on_error_continue: bool,
    interval_seconds: float,
) -> None:
    """Run a single watch-mode iteration with error handling and logging."""
    try:
        iteration_fn()
    except MngError as e:
        if on_error_continue:
            logger.error("Error in iteration (continuing): {}", e)
        else:
            raise
    logger.info("\nWaiting {} seconds until next refresh...", interval_seconds)


def run_watch_loop(
    iteration_fn: Callable[[], None],
    interval_seconds: float,
    *,
    on_error_continue: bool = True,
) -> None:
    """Run a function repeatedly at a specified interval.

    This is used for watch mode in CLI commands like `mng list --watch` and
    `mng gc --watch`. The iteration function is called, then we wait for the
    specified interval before calling it again. This continues until a
    KeyboardInterrupt is raised.
    """
    logger.info("Starting watch mode: refreshing every {} seconds", interval_seconds)
    logger.info("Press Ctrl+C to stop")

    run_periodically(
        fn=lambda: _run_iteration_with_logging(iteration_fn, on_error_continue, interval_seconds),
        interval=float(interval_seconds),
    )
