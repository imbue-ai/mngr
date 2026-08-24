import gc
import signal
import subprocess
import time
import warnings
from io import BytesIO
from threading import Event
from typing import IO
from typing import cast

import pytest

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.errors import ProcessTimeoutError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.subprocess_utils import OutputGatherer
from imbue.concurrency_group.subprocess_utils import PartialOutputContainer
from imbue.concurrency_group.subprocess_utils import _POST_KILL_DRAIN_TIMEOUT_SECONDS
from imbue.concurrency_group.subprocess_utils import _is_timeout
from imbue.concurrency_group.subprocess_utils import _shutdown_popen
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.concurrency_group.test_utils import LONG_SLEEP_SECONDS


def test_check_raises_process_timeout_error_when_timed_out() -> None:
    process = FinishedProcess(
        returncode=None,
        stdout="some output",
        stderr="some error",
        command=("sleep", "100"),
        is_timed_out=True,
        is_output_already_logged=False,
    )

    with pytest.raises(ProcessTimeoutError) as exc_info:
        process.check()

    assert exc_info.value.command == ("sleep", "100")
    assert exc_info.value.stdout == "some output"
    assert exc_info.value.stderr == "some error"


def test_check_raises_process_error_when_nonzero_exit() -> None:
    process = FinishedProcess(
        returncode=42,
        stdout="stdout content",
        stderr="stderr content",
        command=("test_cmd", "arg1"),
        is_timed_out=False,
        is_output_already_logged=False,
    )

    with pytest.raises(ProcessError) as exc_info:
        process.check()

    assert exc_info.value.returncode == 42
    assert exc_info.value.command == ("test_cmd", "arg1")
    assert exc_info.value.stdout == "stdout content"
    assert exc_info.value.stderr == "stderr content"


def test_check_propagates_display_name_and_hides_secret_command() -> None:
    """A ``FinishedProcess`` carrying a ``display_name`` raises a ProcessError that renders
    the name -- not the raw argv -- so a secret argument value stays out of the message,
    while ``.command`` keeps the real argv."""
    process = FinishedProcess(
        returncode=1,
        stdout="",
        stderr="boom",
        command=("modal", "secret", "create", "NAME=s3cr3t-value"),
        is_timed_out=False,
        is_output_already_logged=False,
        display_name="modal secret create NAME=***",
    )

    with pytest.raises(ProcessError) as exc_info:
        process.check()

    assert "s3cr3t-value" not in str(exc_info.value)
    assert exc_info.value.display_command == "modal secret create NAME=***"
    assert exc_info.value.command == ("modal", "secret", "create", "NAME=s3cr3t-value")


def test_check_returns_self_on_success() -> None:
    process = FinishedProcess(
        returncode=0,
        stdout="success output",
        stderr="",
        command=("echo", "hello"),
        is_timed_out=False,
        is_output_already_logged=False,
    )

    result = process.check()

    assert result is process


def test_check_timeout_takes_precedence_over_nonzero_exit() -> None:
    process = FinishedProcess(
        returncode=1,
        stdout="",
        stderr="",
        command=("cmd",),
        is_timed_out=True,
        is_output_already_logged=True,
    )

    with pytest.raises(ProcessTimeoutError):
        process.check()


def test_check_preserves_is_output_already_logged_in_error() -> None:
    process = FinishedProcess(
        returncode=1,
        stdout="",
        stderr="",
        command=("cmd",),
        is_timed_out=False,
        is_output_already_logged=True,
    )

    with pytest.raises(ProcessError) as exc_info:
        process.check()

    assert exc_info.value.is_output_already_logged is True


def test_write_accumulates_output_in_buffer() -> None:
    container = PartialOutputContainer()

    container.write(b"hello ")
    container.write(b"world")

    assert container.get_complete_output() == b"hello world"


def test_write_calls_callback_on_complete_line_ending_with_newline() -> None:
    received_lines: list[str] = []
    container = PartialOutputContainer(on_complete_line=received_lines.append)

    container.write(b"complete line\n")

    assert received_lines == ["complete line\n"]


def test_write_calls_callback_on_complete_line_ending_with_carriage_return() -> None:
    received_lines: list[str] = []
    container = PartialOutputContainer(on_complete_line=received_lines.append)

    container.write(b"complete line\r")

    assert received_lines == ["complete line\r"]


def test_write_does_not_call_callback_for_incomplete_line() -> None:
    received_lines: list[str] = []
    container = PartialOutputContainer(on_complete_line=received_lines.append)

    container.write(b"incomplete line without newline")

    assert received_lines == []
    assert container.in_progress_line == bytearray(b"incomplete line without newline")


def test_write_accumulates_partial_line_across_writes() -> None:
    received_lines: list[str] = []
    container = PartialOutputContainer(on_complete_line=received_lines.append)

    container.write(b"first ")
    container.write(b"part ")
    container.write(b"final\n")

    assert received_lines == ["first part final\n"]


def test_write_handles_multiple_lines_in_single_write() -> None:
    received_lines: list[str] = []
    container = PartialOutputContainer(on_complete_line=received_lines.append)

    container.write(b"line1\nline2\nline3\n")

    assert received_lines == ["line1\n", "line2\n", "line3\n"]


def test_write_handles_mixed_complete_and_incomplete_lines() -> None:
    received_lines: list[str] = []
    container = PartialOutputContainer(on_complete_line=received_lines.append)

    container.write(b"complete\nincomplete")

    assert received_lines == ["complete\n"]
    assert container.in_progress_line == bytearray(b"incomplete")


def test_write_handles_empty_bytes() -> None:
    received_lines: list[str] = []
    container = PartialOutputContainer(on_complete_line=received_lines.append)

    container.write(b"")

    assert received_lines == []
    assert container.get_complete_output() == b""


def test_write_with_no_callback_just_accumulates() -> None:
    container = PartialOutputContainer(on_complete_line=None)

    container.write(b"line1\nline2\n")

    assert container.get_complete_output() == b"line1\nline2\n"


def test_write_handles_utf8_characters() -> None:
    received_lines: list[str] = []
    container = PartialOutputContainer(on_complete_line=received_lines.append)

    container.write("unicode: \u00e9\u00e8\u00ea\n".encode("utf-8"))

    assert received_lines == ["unicode: \u00e9\u00e8\u00ea\n"]


def test_write_handles_invalid_utf8_with_replacement() -> None:
    received_lines: list[str] = []
    container = PartialOutputContainer(on_complete_line=received_lines.append)

    container.write(b"invalid: \xff\xfe\n")

    assert len(received_lines) == 1
    assert "invalid:" in received_lines[0]


def test_is_timeout_returns_false_when_timeout_is_none() -> None:
    assert _is_timeout(None) is False


def test_is_timeout_returns_true_when_time_has_passed() -> None:
    past_time = time.monotonic() - 10.0
    assert _is_timeout(past_time) is True


def test_is_timeout_returns_false_when_time_has_not_passed() -> None:
    future_time = time.monotonic() + 100.0
    assert _is_timeout(future_time) is False


def test_is_timeout_reads_the_clock_a_suspended_machine_does_not_advance() -> None:
    """The deadline is monotonic, so time the machine was suspended for is not spent.

    A process cannot notice its own deadline while it is frozen, so a wall-clock
    deadline hands the whole budget to a sleep: two fifteen-minute suspends burn
    a twenty-one minute cap in a couple of hundred seconds of running time, and
    the command is killed and reported as timed out at the wake. Only a
    monotonic reading answers False for a monotonic deadline still in the
    future -- wall clock is epoch seconds, far past any uptime reading, so it
    would call this one expired.
    """
    assert _is_timeout(time.monotonic() + 100.0) is False


def test_shutdown_popen_terminates_with_sigterm_and_returns_signal_returncode() -> None:
    # A process that dies cleanly on SIGTERM must be reaped within the shutdown timeout, and
    # _shutdown_popen must return its signal-based returncode (negative SIGTERM on POSIX) rather than
    # escalating to SIGKILL. Asserting the exact signal (not merely "not None") pins down that the
    # graceful-terminate path was taken; the SIGKILL-escalation path is covered separately by
    # test_shutdown_popen_raises_when_process_cannot_be_killed below.
    process = subprocess.Popen(
        ["sleep", LONG_SLEEP_SECONDS],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    returncode = _shutdown_popen(process, shutdown_timeout_sec=5.0, reason="the test requested shutdown")

    assert returncode == -signal.SIGTERM
    assert process.poll() == -signal.SIGTERM


def test_shutdown_popen_reaps_already_exited_process_with_its_own_exit_code() -> None:
    # A process that finished before the shutdown request must be reaped with its own
    # exit code rather than reported as SIGTERMed: single-use workers routinely exit
    # on their own right before the parent's cleanup reaches them.
    process = subprocess.Popen(
        ["true"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    process.wait(timeout=5)

    returncode = _shutdown_popen(process, shutdown_timeout_sec=5.0, reason="the test requested shutdown")

    assert returncode == 0


def test_timed_out_process_dying_words_are_captured() -> None:
    """Output written while the child handles its shutdown signal reaches the result.

    A timeout kill's final output is the tail that diagnoses where the command
    was stuck, and it is written after the poll loop has stopped gathering --
    only the post-kill drain picks it up.
    """
    # The sleep runs in the background with an interruptible ``wait``: a
    # foreground sleep would defer the TERM trap until the sleep finished.
    script = (
        "trap 'kill $child 2>/dev/null; echo dying-words; exit 1' TERM; echo started; sleep 30 & child=$!; wait $child"
    )
    finished = run_local_command_modern_version(
        ["sh", "-c", script],
        is_checked=False,
        timeout=0.5,
    )
    assert finished.is_timed_out
    assert "started" in finished.stdout
    assert "dying-words" in finished.stdout


def test_shutdown_killed_process_dying_words_are_captured() -> None:
    """Output written while the child handles a shutdown-requested kill also reaches the result.

    The post-kill drain must ignore the shutdown event: it is set in exactly
    this case, and the mid-loop gathers stop once it is set, so only the drain
    can pick up what the child printed while dying. The shutdown is requested
    from the output callback on the child's own trap-armed line, so the TERM
    always reaches an armed handler without any sleep-and-poll.
    """
    script = "trap 'kill $child 2>/dev/null; echo dying-words; exit 1' TERM; echo trap-armed; sleep 30 & child=$!; wait $child"
    shutdown_event = Event()

    def request_shutdown_once_trap_is_armed(line: str, is_stdout: bool) -> None:
        if "trap-armed" in line:
            shutdown_event.set()

    finished = run_local_command_modern_version(
        ["sh", "-c", script],
        is_checked=False,
        shutdown_event=shutdown_event,
        trace_output=True,
        trace_on_line_callback=request_shutdown_once_trap_is_armed,
    )
    assert not finished.is_timed_out
    assert "trap-armed" in finished.stdout
    assert "dying-words" in finished.stdout


class _EndlessStream:
    """Pipe stand-in that always has another full chunk ready, like a live writer would."""

    def read(self, size: int) -> bytes:
        return b"x" * size


def test_post_kill_drain_gives_up_on_a_stream_that_never_runs_dry() -> None:
    """The post-kill drain is bounded even when the pipes keep yielding.

    Only the direct child is signalled, so a grandchild that inherited the pipes
    can keep the write ends open and keep writing. The drain has no shutdown
    short-circuit to fall back on, so without its own deadline it would never
    return and the kill path -- the one place a hang is least acceptable --
    would hang.
    """
    gatherer = OutputGatherer(
        stdout=cast(IO[bytes], _EndlessStream()),
        stderr=cast(IO[bytes], _EndlessStream()),
        stdout_container=PartialOutputContainer(is_output_accumulated=False),
        stderr_container=PartialOutputContainer(is_output_accumulated=False),
        shutdown_event=Event(),
    )

    started_at = time.monotonic()
    gatherer.gather_output(is_draining_after_exit=True)
    elapsed = time.monotonic() - started_at

    assert elapsed >= _POST_KILL_DRAIN_TIMEOUT_SECONDS
    assert elapsed < _POST_KILL_DRAIN_TIMEOUT_SECONDS + 10.0


def test_gather_output_reads_from_stdout_and_stderr() -> None:
    stdout_data = b"stdout content\n"
    stderr_data = b"stderr content\n"

    stdout_io = BytesIO(stdout_data)
    stderr_io = BytesIO(stderr_data)

    stdout_container = PartialOutputContainer()
    stderr_container = PartialOutputContainer()
    shutdown_event = Event()

    gatherer = OutputGatherer(
        stdout=stdout_io,
        stderr=stderr_io,
        stdout_container=stdout_container,
        stderr_container=stderr_container,
        shutdown_event=shutdown_event,
    )

    gatherer.gather_output()

    stdout_output, stderr_output = gatherer.get_output()
    assert stdout_output == stdout_data
    assert stderr_output == stderr_data


def test_gather_output_stops_when_shutdown_event_is_set() -> None:
    class InfiniteReader:
        """A reader that returns data forever until stopped."""

        def __init__(self) -> None:
            self.read_count = 0

        def read(self, size: int) -> bytes:
            self.read_count += 1
            if self.read_count > 100:
                return b""
            return b"x" * 10

    stdout_reader = InfiniteReader()
    stderr_reader = InfiniteReader()

    stdout_container = PartialOutputContainer()
    stderr_container = PartialOutputContainer()
    shutdown_event = Event()
    shutdown_event.set()

    gatherer = OutputGatherer(
        stdout=stdout_reader,  # ty: ignore[invalid-argument-type]
        stderr=stderr_reader,  # ty: ignore[invalid-argument-type]
        stdout_container=stdout_container,
        stderr_container=stderr_container,
        shutdown_event=shutdown_event,
    )

    gatherer.gather_output()

    assert stdout_reader.read_count == 0


def test_get_incomplete_lines_returns_partial_content() -> None:
    stdout_io = BytesIO(b"complete\nincomplete_stdout")
    stderr_io = BytesIO(b"done\nincomplete_stderr")

    stdout_container = PartialOutputContainer(on_complete_line=lambda _: None)
    stderr_container = PartialOutputContainer(on_complete_line=lambda _: None)
    shutdown_event = Event()

    gatherer = OutputGatherer(
        stdout=stdout_io,
        stderr=stderr_io,
        stdout_container=stdout_container,
        stderr_container=stderr_container,
        shutdown_event=shutdown_event,
    )

    gatherer.gather_output()

    incomplete_stdout, incomplete_stderr = gatherer.get_incomplete_lines()
    assert incomplete_stdout == "incomplete_stdout"
    assert incomplete_stderr == "incomplete_stderr"


def test_gather_output_handles_none_reads() -> None:
    class NoneReader:
        """A reader that returns None (non-blocking with no data)."""

        def read(self, size: int) -> bytes | None:
            return None

    stdout_reader = NoneReader()
    stderr_reader = NoneReader()

    stdout_container = PartialOutputContainer()
    stderr_container = PartialOutputContainer()
    shutdown_event = Event()

    gatherer = OutputGatherer(
        stdout=stdout_reader,  # ty: ignore[invalid-argument-type]
        stderr=stderr_reader,  # ty: ignore[invalid-argument-type]
        stdout_container=stdout_container,
        stderr_container=stderr_container,
        shutdown_event=shutdown_event,
    )

    gatherer.gather_output()

    stdout_output, stderr_output = gatherer.get_output()
    assert stdout_output == b""
    assert stderr_output == b""


def test_run_local_command_closes_subprocess_pipes() -> None:
    """Verify stdout/stderr pipes are closed after command completes, not left for GC."""
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        run_local_command_modern_version(["echo", "hello"])
        gc.collect()

    resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert resource_warnings == [], (
        f"Subprocess pipes not closed explicitly; got {len(resource_warnings)} ResourceWarning(s): "
        + ", ".join(str(w.message) for w in resource_warnings)
    )
