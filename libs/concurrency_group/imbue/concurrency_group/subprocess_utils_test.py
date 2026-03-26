import gc
import subprocess
import time
import warnings
from io import BytesIO
from threading import Event

import pytest

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.errors import ProcessTimeoutError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.subprocess_utils import OutputGatherer
from imbue.concurrency_group.subprocess_utils import PartialOutputContainer
from imbue.concurrency_group.subprocess_utils import _is_timeout
from imbue.concurrency_group.subprocess_utils import _shutdown_popen
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version


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
    past_time = time.time() - 10.0
    assert _is_timeout(past_time) is True


def test_is_timeout_returns_false_when_time_has_not_passed() -> None:
    future_time = time.time() + 100.0
    assert _is_timeout(future_time) is False


def test_shutdown_popen_terminates_process_with_sigterm() -> None:
    process = subprocess.Popen(
        ["sleep", "30"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    returncode = _shutdown_popen(process, "sleep 30", shutdown_timeout_sec=5.0)

    assert returncode is not None
    assert process.poll() is not None


def test_shutdown_popen_returns_returncode_after_terminate() -> None:
    # Use a simple sleep command that terminates cleanly on SIGTERM
    # Short sleep time to avoid hanging if shutdown fails
    process = subprocess.Popen(
        ["sleep", "5"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    returncode = _shutdown_popen(process, "sleep command", shutdown_timeout_sec=2.0)

    assert returncode is not None
    assert process.poll() is not None


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
        stdout=stdout_reader,  # type: ignore[arg-type]
        stderr=stderr_reader,  # type: ignore[arg-type]
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
        stdout=stdout_reader,  # type: ignore[arg-type]
        stderr=stderr_reader,  # type: ignore[arg-type]
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
