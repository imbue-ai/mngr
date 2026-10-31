import json
import subprocess
import sys
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import TypeVar

_SampleT = TypeVar("_SampleT")


def run_command(
    argv: Sequence[str],
    timeout_seconds: float,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` to completion with its output captured; a tool that cannot start or overruns reads as exit 127."""
    try:
        return subprocess.run(
            list(argv), capture_output=True, text=True, check=False, timeout=timeout_seconds, cwd=cwd, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(
            list(argv), returncode=127, stdout="", stderr="failed to run %s: %s" % (argv[0], e)
        )


def argv_value(argv: Sequence[str], flag: str, default: str) -> str:
    """The value following ``flag`` in a program's arguments, or ``default`` when it is absent."""
    for idx, token in enumerate(argv):
        if token == flag and idx + 1 < len(argv):
            return argv[idx + 1]
    return default


def has_argv_flag(argv: Sequence[str], flag: str) -> bool:
    return flag in argv


def poll_until(
    sample: Callable[[], _SampleT],
    is_done: Callable[[_SampleT], bool],
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> _SampleT:
    """Take samples until one is done or the timeout passes, and return the last one taken."""
    deadline = time.monotonic() + timeout_seconds
    latest = sample()
    while not is_done(latest) and time.monotonic() < deadline:
        time.sleep(poll_interval_seconds)
        latest = sample()
    return latest


def as_json_object(value: object) -> dict[str, object] | None:
    """``value`` as a JSON object, or None when it is any other JSON value."""
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def as_json_object_list(value: object) -> list[dict[str, object]]:
    """The JSON objects in ``value`` when it is a JSON array; empty for any other JSON value."""
    if not isinstance(value, list):
        return []
    return [item for item in (as_json_object(element) for element in value) if item is not None]


def emit_marker_json(marker: str, payload: Mapping[str, object]) -> None:
    """Print the program's answer: one ``marker``-prefixed JSON line the app finds in noisy output."""
    sys.stdout.write(marker + json.dumps(payload) + "\n")
    sys.stdout.flush()


def write_progress_line(message: str) -> None:
    """Print one live progress line, which the app streams into the operation's log."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()
