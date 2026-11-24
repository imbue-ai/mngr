from collections.abc import Generator
from pathlib import Path

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


@pytest.fixture
def source_and_work_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Create source and work directories for work_dir_extra_paths tests.

    Returns (source_dir, work_dir), both already created under tmp_path.
    """
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return (source_dir, work_dir)
