import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest

_KILL_PROCESSES_UNDER: Final[Path] = Path(__file__).parent / "kill-processes-under.sh"
_WRITER_ROOT_ENV_VAR: Final[str] = "KILL_PROCESSES_UNDER_TEST_ROOT"
_HELD_FILE_SIZE: Final[int] = 4096

# Takes one kind of reference to a file under the root (whose path it reads from its
# environment, never its argv), closes its stdout once that reference is in place, and on
# stdin EOF recreates a directory under the root.
_WRITER_PROGRAM: Final[str] = f"""
import ctypes
import mmap
import os
import sys

root = os.environ["{_WRITER_ROOT_ENV_VAR}"]
held_path = os.path.join(root, "production", ".venv", "lib", "held.so")
reference_kind = sys.argv[1]
if reference_kind == "mapped_file":
    libc = ctypes.CDLL(None)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    held_fd = os.open(held_path, os.O_RDONLY)
    address = libc.mmap(None, {_HELD_FILE_SIZE}, mmap.PROT_READ, mmap.MAP_SHARED, held_fd, 0)
    os.close(held_fd)
    assert address not in (None, ctypes.c_void_p(-1).value), "mmap failed"
elif reference_kind == "open_file":
    held_file = open(held_path, "rb")
elif reference_kind == "cwd":
    os.chdir(os.path.dirname(held_path))
else:
    sys.exit("unknown reference kind: " + reference_kind)
os.close(sys.stdout.fileno())
sys.stdin.read()
os.makedirs(os.path.join(root, "production", "mngr", "events"), exist_ok=True)
"""


def _make_root(parent: Path, name: str) -> Path:
    root = parent / name
    held_path = root / "production" / ".venv" / "lib" / "held.so"
    held_path.parent.mkdir(parents=True)
    held_path.write_bytes(b"\0" * _HELD_FILE_SIZE)
    return root


@contextmanager
def _running_writer(root: Path, reference_kind: str, cwd: Path) -> Iterator[subprocess.Popen[str]]:
    with subprocess.Popen(
        [sys.executable, "-c", _WRITER_PROGRAM, reference_kind],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env={**os.environ, _WRITER_ROOT_ENV_VAR: str(root)},
    ) as writer:
        try:
            assert writer.stdout is not None
            writer.stdout.read()
            yield writer
        finally:
            writer.kill()


def _kill_processes_under(*roots: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_KILL_PROCESSES_UNDER), *(str(root) for root in roots)],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("reference_kind", ["mapped_file", "open_file", "cwd"])
def test_kill_processes_under_returns_only_after_every_process_with_a_file_under_the_root_exited(
    tmp_path: Path, reference_kind: str
) -> None:
    root = _make_root(tmp_path, "Imbue Studio")
    with _running_writer(root, reference_kind, cwd=tmp_path) as writer:
        result = _kill_processes_under(root)
        assert result.returncode == 0, result.stderr
        assert writer.poll() == -signal.SIGKILL

        # A writer that outlived the kill would recreate the root on this EOF.
        shutil.rmtree(root)
        assert writer.stdin is not None
        writer.stdin.close()
        writer.wait(timeout=30)
    assert not root.exists()


def test_kill_processes_under_leaves_a_process_in_a_sibling_directory_with_the_same_name_prefix_running(
    tmp_path: Path,
) -> None:
    root = _make_root(tmp_path, "Imbue Studio")
    sibling = _make_root(tmp_path, "Imbue Studio Beta")
    with _running_writer(sibling, "open_file", cwd=tmp_path) as bystander:
        result = _kill_processes_under(root)
        assert result.returncode == 0, result.stderr
        assert bystander.poll() is None


@pytest.mark.parametrize("root", ["", "/", "relative/path"])
def test_kill_processes_under_rejects_a_root_that_is_not_an_absolute_path_below_slash(root: str) -> None:
    result = subprocess.run(["bash", str(_KILL_PROCESSES_UNDER), root], capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert f"'{root}' is not an absolute path below /" in result.stderr
