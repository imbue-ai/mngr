from collections.abc import Generator

import pytest
from loguru import logger


@pytest.fixture(autouse=True)
def _reset_loguru() -> Generator[None, None, None]:
    """Reset loguru handlers before and after each test to prevent handler leakage."""
    logger.remove()
    yield
    logger.remove()
