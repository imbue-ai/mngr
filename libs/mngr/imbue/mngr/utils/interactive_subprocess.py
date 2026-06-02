import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import IO

from imbue.mngr.errors import MngrError


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


def run_command_in_terminal(
    cmd: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    error_class: type[MngrError] = MngrError,
) -> None:
    """Run ``cmd`` with the user's terminal stdio (no redirection); raise on non-zero.

    Convenience wrapper around :func:`run_interactive_subprocess` for the common
    CLI case where mngr does some setup, hands off to an external tool whose
    output should flow straight to the terminal (progress, errors, prompts,
    pager-style output), then waits for it to exit -- leaving the caller free
    to run cleanup work afterwards.

    On non-zero exit, raises ``error_class`` (defaulting to :class:`MngrError`)
    with the command name and exit status in the message.
    """
    result = run_interactive_subprocess(cmd, env=env)
    if result.returncode != 0:
        raise error_class(f"{cmd[0]} exited with status {result.returncode}")


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
