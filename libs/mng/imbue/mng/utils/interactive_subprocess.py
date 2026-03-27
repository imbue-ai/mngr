import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import IO


def run_interactive_subprocess(
    command: str | Sequence[str],
    *,
    stdin: int | IO[Any] | None = None,
    stdout: int | IO[Any] | None = None,
    stderr: int | IO[Any] | None = None,
    shell: bool = False,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run a subprocess that requires interactive terminal access.

    These bypass ConcurrencyGroup because they need direct terminal control
    (stdin/stdout/stderr passthrough to the user's terminal).
    """
    return subprocess.run(
        command,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        shell=shell,
        cwd=cwd,
        env=env,
        check=check,
        timeout=timeout,
    )


def popen_interactive_subprocess(
    command: str | Sequence[str],
    *,
    stdin: int | IO[Any] | None = None,
    stdout: int | IO[Any] | None = None,
    stderr: int | IO[Any] | None = None,
    shell: bool = False,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[Any]:
    """Open a subprocess that requires interactive terminal access.

    These bypass ConcurrencyGroup because they need direct terminal control
    (stdin/stdout/stderr passthrough to the user's terminal).
    """
    return subprocess.Popen(
        command,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        shell=shell,
        cwd=cwd,
        env=env,
    )
