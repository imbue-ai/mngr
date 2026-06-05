from collections.abc import Generator

import pytest
from loguru import logger


@pytest.fixture(autouse=True)
def _reset_loguru() -> Generator[None, None, None]:
    """Reset loguru handlers before and after each test to prevent handler leakage."""
    logger.remove()
    yield
    logger.remove()


@pytest.fixture()
def log_warnings() -> Generator[list[str], None, None]:
    """Capture loguru WARNING-level messages so tests can assert a warning fired.

    Installed after the autouse ``_reset_loguru`` fixture clears handlers, so the
    capture sink is the only handler for the duration of the test. Teardown
    tolerates the handler already being gone (e.g. if the code under test calls
    ``logger.remove()`` itself).
    """
    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="WARNING", format="{message}")
    try:
        yield messages
    finally:
        try:
            logger.remove(handler_id)
        except ValueError:
            pass
