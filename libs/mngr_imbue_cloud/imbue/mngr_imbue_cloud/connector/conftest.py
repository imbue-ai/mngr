from collections.abc import Generator

import pytest

from imbue.imbue_common.profiling import disable
from imbue.imbue_common.profiling import enable_collector


@pytest.fixture
def collecting_profiler() -> Generator[None, None, None]:
    """Run the test with the process-wide profiler collecting events, returning it to the no-op afterwards."""
    enable_collector()
    try:
        yield
    finally:
        disable()
