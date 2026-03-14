from collections.abc import Generator

import pytest
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup


@pytest.fixture(autouse=True)
def _reset_loguru() -> Generator[None, None, None]:
    """Reset loguru handlers before and after each test to prevent handler leakage."""
    logger.remove()
    yield
    logger.remove()


@pytest.fixture()
def test_concurrency_group() -> Generator[ConcurrencyGroup, None, None]:
    """Provide a real ConcurrencyGroup for tests that use ConcurrencyGroupExecutor."""
    cg = ConcurrencyGroup(name="test")
    with cg:
        yield cg
