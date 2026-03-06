import subprocess
from pathlib import Path

from imbue.skitwright.data_types import CommandResult


def run_command(
    command: str,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
) -> CommandResult:
    """Execute a shell command and return a structured result."""
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command=command,
            exit_code=124,
            stdout=exc.stdout or "" if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace"),
            stderr=f"Command timed out after {timeout}s",
        )

    return CommandResult(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
