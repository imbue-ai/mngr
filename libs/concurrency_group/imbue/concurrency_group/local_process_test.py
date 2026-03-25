import tempfile
import threading
from pathlib import Path
from queue import Empty
from queue import Queue
from subprocess import TimeoutExpired
from threading import Event
from time import monotonic

import pytest

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.event_utils import CompoundEvent
from imbue.concurrency_group.local_process import RunningProcess
from imbue.concurrency_group.local_process import run_background
from imbue.concurrency_group.test_utils import poll_until


def test_run_background_simple_command() -> None:
    """Test running a simple echo command in background."""
    process = run_background(["echo", "hello world"])

    stdout, stderr = process.wait_and_read(timeout=5.0)

    assert process.returncode == 0
    assert stdout.strip() == "hello world"
    assert stderr == ""


def test_run_background_with_queue() -> None:
    """Test that output is properly sent to the queue."""
    output_queue: Queue[tuple[str, bool]] = Queue()
    process = run_background(["echo", "test output"], output_queue=output_queue)

    process.wait(timeout=5.0)

    # Read from queue
    line, is_stdout = output_queue.get(timeout=1.0)
    assert line == "test output\n"
    assert is_stdout

    # Queue should be empty now
    with pytest.raises(Empty):
        output_queue.get(block=False)

    assert process.returncode == 0


def test_run_background_multiline_output() -> None:
    """Test background process with multiple lines of output."""
    process = run_background(["sh", "-c", "echo 'line1'; echo 'line2'; echo 'line3'"])

    stdout, stderr = process.wait_and_read(timeout=5.0)

    assert process.returncode == 0
    lines = stdout.strip().split("\n")
    assert len(lines) == 3
    assert lines[0] == "line1"
    assert lines[1] == "line2"
    assert lines[2] == "line3"


def test_run_background_mixed_stdout_stderr() -> None:
    """Test background process that writes to both stdout and stderr."""
    process = run_background(["sh", "-c", "echo 'stdout line'; echo 'stderr line' >&2"])

    stdout, stderr = process.wait_and_read(timeout=5.0)

    assert process.returncode == 0
    assert stdout.strip() == "stdout line"
    assert stderr.strip() == "stderr line"


def test_run_background_real_time_queue() -> None:
    """Test that output is available in real-time via queue."""
    output_queue: Queue[tuple[str, bool]] = Queue()
    start_time = monotonic()

    # Command that outputs with delays
    process = run_background(["sh", "-c", "echo 'immediate'; sleep 0.1; echo 'delayed'"], output_queue=output_queue)

    # Get first line immediately
    line1, is_stdout1 = output_queue.get(timeout=0.2)
    time1 = monotonic() - start_time

    # Get second line after delay
    line2, is_stdout2 = output_queue.get(timeout=0.5)
    time2 = monotonic() - start_time

    process.wait(timeout=1.0)

    assert line1 == "immediate\n"
    assert is_stdout1
    assert time1 < 0.3  # First line should come quickly

    assert line2 == "delayed\n"
    assert is_stdout2
    assert time2 > 0.08  # Second line should come after delay

    assert process.returncode == 0


def test_run_background_poll_and_is_finished() -> None:
    """Test polling and checking if process is finished."""
    # Fast command
    process = run_background(["echo", "quick"])

    # Wait for completion
    process.wait(timeout=5.0)

    # After wait, should be finished
    assert process.is_finished()
    assert process.poll() == 0
    assert process.returncode == 0


def test_run_background_long_running_poll() -> None:
    """Test polling a long-running process."""
    process = run_background(["sleep", "0.2"])

    # Should not be finished immediately
    assert not process.is_finished()
    assert process.poll() is None
    assert process.returncode is None

    # Wait for completion
    process.wait(timeout=2.0)

    # Should be finished now
    assert process.is_finished()
    assert process.poll() == 0
    assert process.returncode == 0


def test_run_background_terminate() -> None:
    """Test terminating a background process."""
    process = run_background(["sleep", "10"])

    # Wait until the process is running
    assert poll_until(lambda: not process.is_finished(), timeout=5.0)

    # Terminate it
    process.terminate(force_kill_seconds=2.0)

    # Should be terminated now
    assert process.is_finished()
    # Return code will be non-zero due to termination
    assert process.returncode != 0


def test_run_background_wait_timeout() -> None:
    """Test that wait() properly times out."""
    process = run_background(["sleep", "10"])

    start_time = monotonic()
    with pytest.raises(TimeoutExpired):  # subprocess.TimeoutExpired
        process.wait(timeout=0.1)

    elapsed = monotonic() - start_time
    assert elapsed < 0.5  # Should timeout quickly

    # Process should still be running
    assert not process.is_finished()

    # Clean up
    process.terminate()


def test_run_background_with_cwd() -> None:
    """Test running background process in specific directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create test files
        (tmpdir_path / "test1.txt").touch()
        (tmpdir_path / "test2.txt").touch()

        process = run_background(["ls"], cwd=tmpdir_path)

        stdout, stderr = process.wait_and_read(timeout=5.0)

        assert process.returncode == 0
        assert "test1.txt" in stdout
        assert "test2.txt" in stdout


def test_run_background_non_zero_exit() -> None:
    """Test background process with non-zero exit code."""
    process = run_background(["sh", "-c", "echo 'error message' >&2; exit 42"])

    stdout, stderr = process.wait_and_read(timeout=5.0)

    assert process.returncode == 42
    assert stdout == ""
    assert stderr.strip() == "error message"


def test_run_background_non_zero_exit_with_checked_output() -> None:
    """Test background process with non-zero exit code."""
    process = run_background(["sh", "-c", "echo 'error message' >&2; exit 42"], is_checked=True)
    with pytest.raises(ProcessError):
        process.wait()

    stdout, stderr = process.read_stdout(), process.read_stderr()
    assert process.returncode == 42
    assert stdout == ""
    assert stderr.strip() == "error message"


def test_run_background_command_not_found() -> None:
    """Test running a non-existent command."""
    with pytest.raises(ProcessSetupError):
        run_background(["nonexistent_command_xyz123"])


def test_run_background_shutdown_event() -> None:
    """Test using shutdown event to interrupt background process."""
    shutdown_event = Event()

    process = run_background(["sleep", "10"], shutdown_event=shutdown_event, shutdown_timeout_sec=0.2)

    # Wait until the process is running
    assert poll_until(lambda: not process.is_finished(), timeout=5.0)

    # Trigger shutdown
    shutdown_event.set()

    # Wait for the process to actually complete
    process.wait(timeout=5.0)

    # Process should be terminated
    assert process.is_finished()
    assert process.returncode != 0


def test_run_background_compound_shutdown_event() -> None:
    """Test using CompoundEvent for shutdown."""
    event1 = Event()
    event2 = Event()
    compound_event = CompoundEvent([event1, event2])

    # RemoteRunningProcess lets shutdown_event be a ReadOnlyEvent, including CompoundEvent, but RunningProcess only allows MutableEvent
    process: RunningProcess = run_background(
        ["sleep", "10"],
        shutdown_event=compound_event,  # type: ignore[arg-type]
        shutdown_timeout_sec=0.2,
    )

    # Wait until the process is running
    assert poll_until(lambda: not process.is_finished(), timeout=5.0)

    # Trigger one of the compound events
    event2.set()

    # Wait for the process to actually complete
    process.wait(timeout=5.0)

    # Process should be terminated
    assert process.is_finished()
    assert process.returncode != 0


def test_run_background_read_methods() -> None:
    """Test read_stdout and read_stderr methods."""
    process = run_background(["sh", "-c", "echo 'stdout1'; echo 'stderr1' >&2; echo 'stdout2'; echo 'stderr2' >&2"])

    process.wait(timeout=5.0)

    stdout = process.read_stdout()
    stderr = process.read_stderr()

    assert "stdout1" in stdout
    assert "stdout2" in stdout
    assert "stderr1" in stderr
    assert "stderr2" in stderr

    assert process.returncode == 0


def test_run_background_get_queue() -> None:
    """Test get_queue method returns the correct queue."""
    custom_queue: Queue[tuple[str, bool]] = Queue()
    process = run_background(["echo", "test"], output_queue=custom_queue)

    # get_queue should return our custom queue
    assert process.get_queue() is custom_queue

    process.wait(timeout=5.0)

    # Output should be in our custom queue
    line, is_stdout = custom_queue.get(timeout=1.0)
    assert line == "test\n"
    assert is_stdout


def test_run_background_concurrent_processes() -> None:
    """Test running multiple background processes concurrently."""
    processes = []

    # Start multiple processes
    for i in range(3):
        process = run_background(["sh", "-c", f"sleep 0.{i}; echo 'Process {i}'"])
        processes.append(process)

    # Wait for all to complete
    results = []
    for i, process in enumerate(processes):
        stdout, stderr = process.wait_and_read(timeout=5.0)
        results.append((i, stdout.strip()))
        assert process.returncode == 0

    # Verify all processes completed successfully
    for i, output in results:
        assert output == f"Process {i}"


def test_run_background_large_output() -> None:
    """Test handling of large output."""
    # Generate ~100KB of output
    process = run_background(
        ["sh", "-c", 'for i in $(seq 1 2000); do echo "Line $i - This is a longer line to increase output size"; done']
    )

    stdout, stderr = process.wait_and_read(timeout=10.0)

    assert process.returncode == 0
    lines = stdout.strip().split("\n")
    assert len(lines) == 2000
    assert lines[0] == "Line 1 - This is a longer line to increase output size"
    assert lines[1999] == "Line 2000 - This is a longer line to increase output size"


def test_run_background_queue_ordering() -> None:
    """Test that queue preserves output order."""
    output_queue: Queue[tuple[str, bool]] = Queue()

    process = run_background(["sh", "-c", 'for i in 1 2 3 4 5; do echo "Line $i"; done'], output_queue=output_queue)

    process.wait(timeout=5.0)

    # Read all lines from queue
    lines = []
    while not output_queue.empty():
        line, is_stdout = output_queue.get()
        if is_stdout:
            lines.append(line.strip())

    # Verify order
    assert len(lines) == 5
    for i in range(5):
        assert lines[i] == f"Line {i + 1}"


def test_run_background_empty_output() -> None:
    """Test process with no output."""
    process = run_background(["true"])

    stdout, stderr = process.wait_and_read(timeout=5.0)

    assert process.returncode == 0
    assert stdout == ""
    assert stderr == ""


def test_run_background_timeout_parameter() -> None:
    """Test that the timeout parameter is passed to the underlying process."""
    # This should timeout because sleep takes longer than timeout
    process = run_background(["sleep", "10"], timeout=0.1)

    # The process should fail due to timeout
    start_time = monotonic()
    return_code = process.wait(timeout=1.0)
    elapsed = monotonic() - start_time

    # Should have timed out quickly
    assert elapsed < 0.5
    assert return_code != 0


def test_run_background_interleaved_stdout_stderr() -> None:
    """Test that stdout and stderr maintain their order in the queue."""
    output_queue: Queue[tuple[str, bool]] = Queue()

    # Command that interleaves stdout and stderr
    process = run_background(
        ["sh", "-c", "echo 'out1'; echo 'err1' >&2; echo 'out2'; echo 'err2' >&2"], output_queue=output_queue
    )

    process.wait(timeout=5.0)

    # Collect all output
    output = []
    while not output_queue.empty():
        line, is_stdout = output_queue.get()
        output.append((line.strip(), is_stdout))

    assert len(output) == 4
    # Check we got the expected lines (order might vary slightly due to buffering)
    stdout_lines = [line for line, is_stdout in output if is_stdout]
    stderr_lines = [line for line, is_stdout in output if not is_stdout]

    assert sorted(stdout_lines) == ["out1", "out2"]
    assert sorted(stderr_lines) == ["err1", "err2"]


def test_run_background_partial_line_handling() -> None:
    """Test handling of output without trailing newlines."""
    output_queue: Queue[tuple[str, bool]] = Queue()

    # Command that outputs without newline at the end
    process = run_background(["sh", "-c", "printf 'no newline'"], output_queue=output_queue)

    stdout, stderr = process.wait_and_read(timeout=5.0)

    assert process.returncode == 0
    assert stdout == "no newline"

    queue_items = []
    while not output_queue.empty():
        queue_items.append(output_queue.get())

    # If there are items, verify they're correct
    assert len(queue_items) == 1
    line, is_stdout = queue_items[0]
    assert "no newline" in line


def test_run_background_thread_safety() -> None:
    """Test that RunningProcess is thread-safe for concurrent access."""
    process = run_background(["sh", "-c", "for i in 1 2 3; do echo $i; done"])

    results: dict[str, list] = {"poll": [], "is_finished": [], "stdout": [], "stderr": []}
    errors: list[Exception] = []

    def poll_thread() -> None:
        try:
            while not process.is_finished():
                results["poll"].append(process.poll())
        except Exception as e:
            errors.append(e)

    def check_thread() -> None:
        try:
            while not process.is_finished():
                results["is_finished"].append(process.is_finished())
        except Exception as e:
            errors.append(e)

    def read_thread() -> None:
        try:
            while not process.is_finished():
                results["stdout"].append(process.read_stdout())
                results["stderr"].append(process.read_stderr())
        except Exception as e:
            errors.append(e)

    # Start threads
    threads = [
        threading.Thread(target=poll_thread),
        threading.Thread(target=check_thread),
        threading.Thread(target=read_thread),
    ]

    for t in threads:
        t.start()

    # Wait for process and threads
    process.wait(timeout=2.0)

    for t in threads:
        t.join()

    # Check no errors occurred
    assert len(errors) == 0
    assert process.returncode == 0


def test_run_background_shutdown_event_already_set() -> None:
    shutdown_event = Event()
    shutdown_event.set()
    process = run_background(["sleep", "10"], shutdown_event=shutdown_event)
    process.wait(timeout=2.0)
    assert process.is_finished()
    assert process.returncode != 0
