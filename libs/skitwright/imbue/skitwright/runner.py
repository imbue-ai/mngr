import os
import signal
import subprocess
import threading
from io import TextIOWrapper
from pathlib import Path

from imbue.skitwright.data_types import CommandResult
from imbue.skitwright.data_types import OutputLine
from imbue.skitwright.data_types import OutputSource


def _read_lines(
    stream: TextIOWrapper,
    source: OutputSource,
    output_lines: list[OutputLine],
    lock: threading.Lock,
) -> None:
    """Read lines from a stream and append them to the shared output list."""
    for line in stream:
        with lock:
            output_lines.append(OutputLine(source=source, text=line.rstrip("\n")))


def run_command(
    command: str,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
) -> CommandResult:
    """Execute a shell command and return a structured result.

    Captures stdout and stderr in real-time, interleaving lines in the order
    they are produced (line-buffered: each complete line is recorded as it arrives).
    """
    proc = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(cwd),
        process_group=0,
    )

    output_lines: list[OutputLine] = []
    lock = threading.Lock()

    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_thread = threading.Thread(target=_read_lines, args=(proc.stdout, OutputSource.STDOUT, output_lines, lock))
    stderr_thread = threading.Thread(target=_read_lines, args=(proc.stderr, OutputSource.STDERR, output_lines, lock))
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()

    stdout_thread.join()
    stderr_thread.join()

    frozen_lines = tuple(output_lines)

    if timed_out:
        timeout_msg = f"Command timed out after {timeout}s"
        timeout_line = OutputLine(source=OutputSource.STDERR, text=timeout_msg)
        real_stderr = _reconstruct(frozen_lines, OutputSource.STDERR)
        return CommandResult(
            command=command,
            exit_code=124,
            stdout=_reconstruct(frozen_lines, OutputSource.STDOUT),
            stderr=real_stderr + timeout_msg + "\n",
            output_lines=(*frozen_lines, timeout_line),
        )

    return CommandResult(
        command=command,
        exit_code=proc.returncode,
        stdout=_reconstruct(frozen_lines, OutputSource.STDOUT),
        stderr=_reconstruct(frozen_lines, OutputSource.STDERR),
        output_lines=frozen_lines,
    )


def _reconstruct(lines: tuple[OutputLine, ...], source: OutputSource) -> str:
    """Reconstruct a combined string from interleaved output lines for a given source."""
    matching = [line.text for line in lines if line.source == source]
    if not matching:
        return ""
    return "\n".join(matching) + "\n"
