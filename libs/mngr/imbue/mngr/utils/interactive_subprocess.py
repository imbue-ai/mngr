import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import IO


def run_interactive_subprocess(
    command: Sequence[str],
    *,
    stdin: int | IO[Any] | None = None,
    stdout: int | IO[Any] | None = None,
    stderr: int | IO[Any] | None = None,
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
        cwd=cwd,
        env=env,
        check=check,
        timeout=timeout,
    )


def popen_interactive_subprocess(
    command: Sequence[str],
    *,
    stdin: int | IO[Any] | None = None,
    stdout: int | IO[Any] | None = None,
    stderr: int | IO[Any] | None = None,
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
        cwd=cwd,
        env=env,
    )


def popen_interactive_shell_command(
    command_line: str,
    *,
    stdin: int | IO[Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[Any]:
    """Open a user-configured command line through the shell, with terminal access.

    Only for command lines the user wrote themselves (e.g. the ``pager`` config
    setting or ``$PAGER``), where shell syntax such as flags and pipelines is the
    point and the user already controls what runs on their machine. Never pass a
    string built from program or remote data; use popen_interactive_subprocess
    with an argv list instead.
    """
    return subprocess.Popen(  # nosec B602
        command_line,
        stdin=stdin,
        shell=True,
        env=env,
    )
