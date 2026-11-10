from collections.abc import Iterator

import pytest

from imbue.imbue_common.profiling import disable


@pytest.fixture(autouse=True)
def _disable_profiler_after_each_test() -> Iterator[None]:
    """Reset the process-wide profiler so a test that enabled a mode cannot leak it into the next."""
    yield
    disable()
