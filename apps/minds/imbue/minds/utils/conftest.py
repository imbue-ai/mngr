from collections.abc import Generator

import pytest
from loguru import logger


@pytest.fixture()
def _isolated_logger() -> Generator[None, None, None]:
    """Remove all loguru handlers before and after each test to isolate logger state."""
    logger.remove()
    yield
    logger.remove()
