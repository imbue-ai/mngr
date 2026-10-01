import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.utils.malloc_arenas import MNGR_MALLOC_ARENA_MAX
from imbue.mngr.utils.malloc_arenas import is_glibc

_IS_GLIBC: Final[bool] = is_glibc()
_BURST_THREAD_COUNT: Final[int] = 8

# Writes whether the cap was set, starts a burst of threads that are all alive at once and
# each allocate a block small enough to come from a malloc arena (under glibc's mmap
# threshold), then writes glibc's own malloc_info report, which lists one <heap> per arena.
_THREAD_BURST_SCRIPT: Final[str] = """
import ctypes
import sys
import threading
from pathlib import Path

from imbue.mngr.utils.malloc_arenas import cap_malloc_arenas

is_capping, cap_report_path, report_path = sys.argv[1] == "cap", sys.argv[2], sys.argv[3]
thread_count = int(sys.argv[4])
Path(cap_report_path).write_text("capped" if is_capping and cap_malloc_arenas() else "uncapped")
barrier = threading.Barrier(thread_count + 1)
held = []

def allocate_and_hold():
    held.append(bytes(64 * 1024))
    barrier.wait()
    barrier.wait()

threads = [threading.Thread(target=allocate_and_hold) for _ in range(thread_count)]
for thread in threads:
    thread.start()
barrier.wait()
libc = ctypes.CDLL("libc.so.6")
libc.fopen.restype = ctypes.c_void_p
libc.fopen.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
libc.malloc_info.argtypes = [ctypes.c_int, ctypes.c_void_p]
libc.fclose.argtypes = [ctypes.c_void_p]
report = libc.fopen(report_path.encode(), b"w")
libc.malloc_info(0, report)
libc.fclose(report)
barrier.wait()
for thread in threads:
    thread.join()
"""


def _run_thread_burst(tmp_path: Path, is_capping: bool, environ: dict[str, str]) -> tuple[str, int]:
    """Run the thread burst in a fresh interpreter; what the cap reported, and how many arenas glibc lists."""
    cap_report_path = tmp_path / "cap_report.txt"
    report_path = tmp_path / "malloc_info.xml"
    subprocess.run(
        [
            sys.executable,
            "-c",
            _THREAD_BURST_SCRIPT,
            "cap" if is_capping else "none",
            str(cap_report_path),
            str(report_path),
            str(_BURST_THREAD_COUNT),
        ],
        env=environ,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return cap_report_path.read_text(), report_path.read_text().count("<heap nr=")


def _environ_without_arena_settings() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in ("MALLOC_ARENA_MAX", "GLIBC_TUNABLES")}


@pytest.mark.skipif(not _IS_GLIBC, reason="malloc arenas are a glibc allocator feature")
def test_thread_burst_without_the_cap_opens_an_arena_per_thread(tmp_path: Path) -> None:
    cap_report, arena_count = _run_thread_burst(tmp_path, is_capping=False, environ=_environ_without_arena_settings())

    assert cap_report == "uncapped"
    assert arena_count > _BURST_THREAD_COUNT // 2


@pytest.mark.skipif(not _IS_GLIBC, reason="malloc arenas are a glibc allocator feature")
def test_cap_malloc_arenas_keeps_a_thread_burst_within_the_cap(tmp_path: Path) -> None:
    cap_report, arena_count = _run_thread_burst(tmp_path, is_capping=True, environ=_environ_without_arena_settings())

    assert cap_report == "capped"
    assert arena_count <= MNGR_MALLOC_ARENA_MAX


@pytest.mark.skipif(not _IS_GLIBC, reason="malloc arenas are a glibc allocator feature")
def test_cap_malloc_arenas_leaves_an_arena_limit_set_in_the_environment(tmp_path: Path) -> None:
    operator_arena_max = 5
    environ = {**_environ_without_arena_settings(), "MALLOC_ARENA_MAX": str(operator_arena_max)}

    cap_report, arena_count = _run_thread_burst(tmp_path, is_capping=True, environ=environ)

    assert cap_report == "uncapped"
    assert arena_count == operator_arena_max
