import os
import re
import signal
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from multiprocessing.connection import Pipe
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Any
from uuid import uuid4

import click
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.concurrency_group.local_process import run_background
from imbue.imbue_common.profiling import get_profiler
from imbue.imbue_common.profiling import parse_traceparent
from imbue.minds.desktop_client.testing import capture_error_logs
from imbue.minds.desktop_client.testing import flushed_trace_events
from imbue.minds.desktop_client.testing import trace_spans_named
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.mngr_caller import MngrCallerNotInitializedError
from imbue.minds.utils.mngr_caller import _WarmMngrProcess
from imbue.minds.utils.mngr_caller import _WarmProcessStartup
from imbue.minds.utils.mngr_caller import _WarmRequest
from imbue.minds.utils.mngr_caller import _WarmResult
from imbue.minds.utils.mngr_caller import _coerce_exit_code
from imbue.minds.utils.mngr_caller import _command_tokens
from imbue.minds.utils.mngr_caller import _execute_mngr_cli
from imbue.minds.utils.mngr_caller import _serve_request
from imbue.mngr.utils.polling import wait_for

_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_CALLER_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _make_cwd_capturing_command(captured: list[str]) -> click.Command:
    """Build a tiny stand-in CLI that records the process working directory.

    The directory is captured into ``captured`` (rather than printed) so the test
    can assert on the cwd the CLI actually ran in.
    """

    @click.command()
    def _command() -> None:
        captured.append(os.getcwd())

    return _command


@pytest.fixture()
def mngr_caller() -> Iterator[MngrCaller]:
    """An initialized caller whose warm processes are torn down after the test.

    :meth:`MngrCaller.initialize` adopts an externally-owned concurrency group
    (required before any call). A real :meth:`MngrCaller.call` leaves an idle
    warm process waiting on a socket for the next call; ``stop`` terminates it
    and the concurrency group's own teardown reaps anything still tracked, so
    the per-session leak checker does not flag a lingering subprocess.
    """
    caller = MngrCaller()
    with ConcurrencyGroup(name="test-mngr-caller") as concurrency_group:
        caller.initialize(concurrency_group)
        try:
            yield caller
        finally:
            caller.stop()


def test_call_before_initialize_raises() -> None:
    """A call on an uninitialized caller is refused rather than spawning a process."""
    caller = MngrCaller()
    with pytest.raises(MngrCallerNotInitializedError):
        caller.call(["--version"], timeout=1.0)


def test_coerce_exit_code_none_is_success() -> None:
    assert _coerce_exit_code(None) == 0


def test_coerce_exit_code_passes_through_ints() -> None:
    assert _coerce_exit_code(0) == 0
    assert _coerce_exit_code(2) == 2


def test_coerce_exit_code_string_message_is_failure() -> None:
    # click/SystemExit with a string code conventionally means an error.
    assert _coerce_exit_code("boom") == 1


def test_call_result_defaults() -> None:
    result = MngrCallResult(returncode=0)
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.is_timed_out is False


def test_execute_mngr_cli_changes_to_requested_cwd(tmp_path: Path) -> None:
    """A non-None ``cwd`` makes the CLI run from that directory.

    ``_execute_mngr_cli`` runs in the throwaway warm process, so ``os.chdir`` is
    safe there; the test restores its own cwd afterwards.
    """
    captured_cwd: list[str] = []
    original_cwd = Path.cwd()
    try:
        returncode, _stdout, _stderr = _execute_mngr_cli(_make_cwd_capturing_command(captured_cwd), (), {}, tmp_path)
    finally:
        os.chdir(original_cwd)
    assert returncode == 0
    assert Path(captured_cwd[0]).resolve() == tmp_path.resolve()


def test_execute_mngr_cli_keeps_cwd_when_none() -> None:
    """A ``None`` ``cwd`` leaves the working directory untouched."""
    captured_cwd: list[str] = []
    original_cwd = Path.cwd()
    returncode, _stdout, _stderr = _execute_mngr_cli(_make_cwd_capturing_command(captured_cwd), (), {}, None)
    assert returncode == 0
    assert Path(captured_cwd[0]).resolve() == original_cwd.resolve()
    assert Path.cwd() == original_cwd


# These tests spawn a real warm ``mngr`` process (a fresh interpreter that
# imports ``imbue.mngr.main``) and run the CLI in it over a socket. Under CI load
# that cold start routinely exceeds the 10s global pytest-timeout (the call's own
# timeout is 120s), so give them a generous per-test timeout and mark them flaky
# so offload retries a contended cold start rather than failing the run.
@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_call_runs_mngr_version_in_warm_process(mngr_caller: MngrCaller) -> None:
    """End-to-end: a real ``mngr --version`` runs in a warm process.

    This exercises the whole mechanism: spawning a warm process connected by an
    anonymous socketpair, handing it the argv over the socket, running the CLI,
    and capturing stdout/exit-code. ``--version`` is used because it does no
    provider discovery, so the call is fast and deterministic.

    Marked flaky: warm-process cold-start occasionally exceeds the 10s pytest
    timeout under CI load.
    """
    result = mngr_caller.call(["--version"], timeout=120.0)
    assert result.returncode == 0
    assert result.is_timed_out is False
    assert result.is_mngr_output is True
    assert "mngr" in result.stdout


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_call_reports_nonzero_exit_for_unknown_command(mngr_caller: MngrCaller) -> None:
    # Marked flaky: warm-process cold-start occasionally exceeds the 10s pytest
    # timeout under CI load.
    result = mngr_caller.call(["definitely-not-a-real-subcommand"], timeout=120.0)
    assert result.returncode != 0


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_second_call_reuses_pre_spawned_warm_process(mngr_caller: MngrCaller) -> None:
    """After one call, a replacement warm process is already waiting for the next.

    The first call pays the cold-start cost; the second should be served by the
    warm process spawned when the first was claimed. We assert correctness of
    both results (timing is not asserted, to avoid flakiness).
    """
    first_result = mngr_caller.call(["--version"], timeout=120.0)
    assert first_result.returncode == 0
    second_result = mngr_caller.call(["--version"], timeout=120.0)
    assert second_result.returncode == 0
    assert "mngr" in second_result.stdout


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_call_times_out_and_reports_timed_out(mngr_caller: MngrCaller) -> None:
    """A zero timeout surfaces as a timed-out result with a sentinel returncode."""
    result = mngr_caller.call(["--version"], timeout=0.0)
    assert result.is_timed_out is True
    assert result.returncode != 0
    # The stderr here is this caller's own line about the timeout, so callers
    # that quote a failure at the user must be able to tell it from mngr's.
    assert result.is_mngr_output is False


def _make_gated_command(started: threading.Event, release: threading.Event) -> click.Command:
    """Build a stand-in CLI that blocks until ``release`` is set.

    Used to hold the serve path mid-command so a test can inspect the threads
    that are armed while the warm process is busy.
    """

    @click.command()
    def _command() -> None:
        started.set()
        release.wait(timeout=30.0)

    return _command


def test_serve_request_arms_parent_death_watcher() -> None:
    """The warm-server serve path must run under an armed parent-death watcher.

    This is what protects a warm process that is orphaned *while busy*: once a
    request has been read off the socket, a socket EOF can no longer wake it,
    so only the parent-death watcher can dismiss it if the parent dies
    mid-command. The watcher must be armed before the command runs, so we
    assert the thread exists while a gated stand-in command is still blocked,
    then release the command and check the result comes back.

    The watcher's actual firing-on-parent-death behavior is covered by
    ``parent_process_test.py``; here we only verify it is wired in.
    """
    parent_connection, child_connection = Pipe(duplex=True)
    command_started = threading.Event()
    release_command = threading.Event()
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as concurrency_group:
        serve_thread = concurrency_group.start_new_thread(
            target=_serve_request,
            args=(
                child_connection,
                _make_gated_command(command_started, release_command),
                concurrency_group,
                ((), {}, None, None),
                _startup_at(time.perf_counter_ns()),
            ),
            name="serve-request",
            is_checked=False,
        )
        try:
            assert command_started.wait(timeout=10.0)
            wait_for(
                lambda: any(t.thread.name == "parent-death-watcher" for t in concurrency_group._threads),
                timeout=10.0,
                poll_interval=0.05,
                error_message="serve did not arm a parent-death watcher",
            )
            watchers = [t for t in concurrency_group._threads if t.thread.name == "parent-death-watcher"]
            assert len(watchers) == 1
            assert watchers[0].thread.is_alive()
        finally:
            release_command.set()
            serve_thread.join(timeout=10.0)
        assert parent_connection.poll(10.0)
        returncode, _stdout, _stderr, profiling = parent_connection.recv()
        assert returncode == 0
        assert profiling is None
        parent_connection.close()


def _startup_at(base_ns: int) -> _WarmProcessStartup:
    """A warm process's timeline with a 2 ms import that ended 1 ms before the request was picked up."""
    return _WarmProcessStartup(
        module_loaded_ns=base_ns + 1_000_000, import_done_ns=base_ns + 3_000_000, serve_start_ns=base_ns + 4_000_000
    )


def _make_named_command(name: str) -> click.Command:
    """A stand-in CLI with one subcommand, so the served argv carries a command name the trace can use."""

    @click.group()
    def _group() -> None:
        pass

    @_group.command(name=name)
    def _subcommand() -> None:
        pass

    return _group


def _serve_one_request(cli: click.Command, request: _WarmRequest, startup: _WarmProcessStartup) -> _WarmResult:
    """Run ``_serve_request`` on a thread of its own and return what it sent back over the socket."""
    parent_connection, child_connection = Pipe(duplex=True)
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as concurrency_group:
        serve_thread = concurrency_group.start_new_thread(
            target=_serve_request,
            args=(child_connection, cli, concurrency_group, request, startup),
            name="serve-request",
            is_checked=False,
        )
        serve_thread.join(timeout=10.0)
        assert parent_connection.poll(10.0)
        result: _WarmResult = parent_connection.recv()
        parent_connection.close()
        return result


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["imbue_cloud", "shares", "status", "--account", "owner@example.com"], ("imbue_cloud", "shares", "status")),
        (["exec", "agent-0123456789abcdef0123456789abcdef@host-1.docker", "cat x", "--no-start"], ("exec",)),
        (["message", "-m", "hi", "--", "agent"], ("message",)),
        (["--version"], ()),
        ([], ()),
    ],
)
def test_command_tokens_are_the_leading_subcommand_names(argv: list[str], expected: tuple[str, ...]) -> None:
    assert _command_tokens(argv) == expected


def test_serve_request_with_a_profiling_block_records_the_startup_slices_and_the_command_under_the_callers_trace() -> (
    None
):
    # A timeline wholly in the past, so the command's span (opened now) comes after the request's pickup.
    sent_at_ns = time.perf_counter_ns() - 10_000_000
    startup = _startup_at(sent_at_ns)

    returncode, _stdout, _stderr, profiling = _serve_one_request(
        _make_named_command("hello"), (("hello",), {}, None, (_CALLER_TRACEPARENT, sent_at_ns)), startup
    )

    assert returncode == 0
    assert profiling is not None
    drained_events, startup_wait_us, run_us = profiling
    events: list[dict[str, Any]] = drained_events
    assert startup_wait_us == 4_000
    assert run_us >= 0
    assert all(event["pid"] == os.getpid() for event in events)
    assert [event["args"]["name"] for event in events if event["name"] == "process_name"] == ["mngr hello"]
    span_by_name = {event["name"]: event for event in events if event["ph"] == "X"}
    import_span = span_by_name["mngr_caller.import"]
    wait_span = span_by_name["mngr_caller.wait_for_request"]
    root = span_by_name["mngr hello"]
    assert import_span["ts"] == startup.module_loaded_ns // 1000
    assert import_span["dur"] == 2_000
    assert wait_span["ts"] == startup.import_done_ns // 1000
    assert wait_span["dur"] == 1_000
    # The slices follow one another: the import ends before the wait begins, and the wait ends before the command.
    assert import_span["ts"] + import_span["dur"] <= wait_span["ts"]
    assert wait_span["ts"] + wait_span["dur"] <= root["ts"]
    assert root["ts"] >= startup.serve_start_ns // 1000
    assert root["dur"] <= run_us
    assert root["bind_id"] == "00f067aa0ba902b7"
    assert root["flow_in"] is True
    assert root["args"]["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert root["args"]["parent_span_id"] == "00f067aa0ba902b7"
    assert _SPAN_ID_PATTERN.match(root["args"]["span_id"])
    # The request, not the environment, put this process in collector mode.
    assert get_profiler().is_enabled is True


def test_serve_request_without_a_profiling_block_records_nothing() -> None:
    returncode, _stdout, _stderr, profiling = _serve_one_request(
        _make_named_command("hello"), (("hello",), {}, None, None), _startup_at(time.perf_counter_ns())
    )

    assert returncode == 0
    assert profiling is None
    assert get_profiler().is_enabled is False


class _ScriptedWarmProcess:
    """A stand-in for a warm process: a sleeping child the caller can terminate, on a pipe this test answers."""

    def __init__(
        self,
        mngr_caller: MngrCaller,
        answer: _WarmResult | None,
        # Builds the answer once the request has arrived, for a test that stamps the child's events while serving it.
        answer_factory: Callable[[_WarmRequest], _WarmResult] | None = None,
    ) -> None:
        parent_connection, self.connection = Pipe(duplex=True)
        self.running_process = run_background(["sleep", "30"])
        self.received_requests: list[_WarmRequest] = []
        self.answer = answer
        self.answer_factory = answer_factory
        concurrency_group = mngr_caller._get_concurrency_group()
        # The pre-warmed real process gives its idle slot to this one, so the next call claims this one.
        wait_for(
            lambda: mngr_caller._warm_process is not None,
            timeout=30.0,
            poll_interval=0.05,
            error_message="pre-warmed mngr process was never stored as the idle process",
        )
        real_process = mngr_caller._warm_process
        mngr_caller._warm_process = _WarmMngrProcess(
            connection=parent_connection, running_process=self.running_process, concurrency_group=concurrency_group
        )
        assert real_process is not None
        real_process.terminate(is_after_call_timeout=False)
        self.answer_thread = concurrency_group.start_new_thread(
            target=self._answer, name="scripted-warm-process", is_checked=False
        )

    def _answer(self) -> None:
        request = self.connection.recv()
        self.received_requests.append(request)
        answer = self.answer_factory(request) if self.answer_factory is not None else self.answer
        if answer is not None:
            self.connection.send(answer)
            self.connection.close()

    def close(self) -> None:
        self.connection.close()
        self.answer_thread.join(timeout=10.0)
        RunningProcess.terminate(self.running_process, force_kill_seconds=10.0)


@pytest.mark.timeout(60)
def test_call_sends_no_profiling_block_while_profiling_is_off(mngr_caller: MngrCaller) -> None:
    scripted = _ScriptedWarmProcess(mngr_caller, answer=(0, "out", "", None))
    try:
        result = mngr_caller.call(["--version"], timeout=30.0)
    finally:
        scripted.close()

    assert result.returncode == 0
    assert result.stdout == "out"
    assert result.is_mngr_output is True
    assert scripted.received_requests == [(("--version",), {}, None, None)]


@pytest.mark.timeout(60)
def test_call_sends_its_span_as_the_profiling_block_and_the_childs_events_land_under_its_pid(
    mngr_caller: MngrCaller, profile_writer_path: Path
) -> None:
    child_events: list[dict[str, object]] = []

    def answer(_request: _WarmRequest) -> _WarmResult:
        # Stamped while the request is being served, between the parent's send and its receive of this answer.
        stamped_at_us = time.perf_counter_ns() // 1000
        child_events.extend(
            [
                {"ph": "M", "name": "process_name", "pid": 424242, "tid": 7, "args": {"name": "mngr fake"}},
                {
                    "ph": "X",
                    "name": "mngr fake",
                    "cat": "app",
                    "ts": stamped_at_us,
                    "dur": 0,
                    "pid": 424242,
                    "tid": 7,
                    "args": {},
                },
            ]
        )
        return (0, "out", "", (list(child_events), 1_234, 5_678))

    scripted = _ScriptedWarmProcess(mngr_caller, answer=None, answer_factory=answer)
    before_ns = time.perf_counter_ns()
    try:
        result = mngr_caller.call(["imbue_cloud", "shares", "status", "--account", "a@b"], timeout=30.0)
    finally:
        scripted.close()
    after_ns = time.perf_counter_ns()

    assert result.returncode == 0
    [(argv, env_overrides, cwd, profiling)] = scripted.received_requests
    assert argv == ("imbue_cloud", "shares", "status", "--account", "a@b")
    assert profiling is not None
    traceparent, sent_at_ns = profiling
    assert before_ns <= sent_at_ns <= after_ns
    events = flushed_trace_events(profile_writer_path)
    [span] = trace_spans_named(events, "mngr_caller.call")
    parent = parse_traceparent(traceparent)
    assert parent is not None
    # The traceparent names this span, and the span is the source of a flow bound to its own id.
    assert span["bind_id"] == parent.span_id
    assert span["flow_out"] is True
    assert span["args"] == {
        "command": "imbue_cloud shares status",
        "child_pid": 424242,
        "startup_wait_us": 1_234,
        "run_us": 5_678,
    }
    assert [event for event in events if event.get("pid") == 424242] == child_events
    # The child's slice was stamped after the request was sent and before its answer was received, so the parent's
    # span, opened before the send and closed after the receive, encloses it on the shared monotonic clock.
    [child_slice] = [event for event in child_events if event["ph"] == "X"]
    assert span["ts"] <= child_slice["ts"] <= span["ts"] + span["dur"]


@pytest.mark.timeout(60)
def test_call_that_times_out_marks_its_span_timed_out(mngr_caller: MngrCaller, profile_writer_path: Path) -> None:
    scripted = _ScriptedWarmProcess(mngr_caller, answer=None)
    try:
        result = mngr_caller.call(["--version"], timeout=0.2)
    finally:
        scripted.close()

    assert result.is_timed_out is True
    [span] = trace_spans_named(flushed_trace_events(profile_writer_path), "mngr_caller.call")
    assert span["args"] == {"command": "", "is_timed_out": True}
    assert span["flow_out"] is True


@pytest.mark.timeout(60)
def test_warm_process_exits_when_parent_disconnects(mngr_caller: MngrCaller) -> None:
    """A warm process must exit promptly once its parent socket is closed.

    Closing the parent end without sending a request simulates the Imbue Studio backend
    going away (e.g. a hard kill). The warm process's receiver thread observes
    the socket EOF and exits the process on its own, leaving no orphan -- even
    though the process is still paying the slow ``imbue.mngr.main`` import when
    the disconnect happens (the test closes the socket immediately after spawn).
    """
    warm_process = mngr_caller._spawn_warm_process()
    warm_process.connection.close()
    wait_for(
        warm_process.running_process.is_finished,
        timeout=30.0,
        poll_interval=0.05,
        error_message="warm mngr process did not exit after its parent disconnected",
    )
    assert warm_process.running_process.is_finished()


@pytest.mark.timeout(60)
def test_prewarmed_process_is_stored_as_idle(mngr_caller: MngrCaller) -> None:
    """The process pre-warmed by ``initialize`` is parked as the idle one and left running."""
    wait_for(
        lambda: mngr_caller._warm_process is not None,
        timeout=30.0,
        poll_interval=0.05,
        error_message="pre-warmed mngr process was never stored as the idle process",
    )
    idle_process = mngr_caller._warm_process
    assert idle_process is not None
    assert not idle_process.running_process.is_finished()


@pytest.mark.timeout(60)
def test_warm_process_spawned_across_stop_is_terminated_not_stored(mngr_caller: MngrCaller) -> None:
    """A spawn still in flight when ``stop`` runs is terminated, not parked as the idle process.

    Parked, nothing would terminate it and the owning group's exit would time
    out waiting on it.
    """
    warm_process = mngr_caller._spawn_warm_process()
    mngr_caller.stop()
    mngr_caller._store_or_terminate_warm_process(warm_process)
    assert warm_process.running_process.is_finished()


@pytest.mark.timeout(60)
def test_warm_process_that_ignores_sigterm_is_killed_when_terminated(mngr_caller: MngrCaller) -> None:
    """Terminating a warm process that does not act on SIGTERM still ends it before ``terminate`` returns.

    A process that never exits on SIGTERM is the limiting case of one slow to honour
    it on a loaded machine. It has to be gone once ``terminate`` returns, or it
    outlives the owning group's exit timeout. The ignored disposition survives the
    exec into the warm process, and a duplicate of the parent's socket end keeps the
    child from exiting on socket EOF, so only the kill after the grace can end it.
    """
    # Let the pre-warm spawn land first, so it does not also start with SIGTERM ignored.
    wait_for(
        lambda: mngr_caller._warm_process is not None,
        timeout=30.0,
        poll_interval=0.05,
        error_message="pre-warmed mngr process was never stored as the idle process",
    )
    previous_sigterm_handler = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        warm_process = mngr_caller._spawn_warm_process()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm_handler)
    held_socket_fd = os.dup(warm_process.connection.fileno())
    try:
        warm_process.terminate(is_after_call_timeout=False)
        assert warm_process.running_process.is_finished()
    finally:
        os.close(held_socket_fd)


_READER_LOOP_FRAME = "imbue.concurrency_group.subprocess_utils.run_local_command_modern_version"


class _UnstoppableRunningProcess(RunningProcess):
    """Times out every stop without asking the reader thread to shut down, so it stays put for inspection."""

    def terminate(self, force_kill_seconds: float = 5.0) -> None:
        raise TimeoutExpired(self.command, force_kill_seconds)


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_warm_process_stop_timeout_reports_clocks_and_reader_stack() -> None:
    """A stop that times out records what tells a suspend, an unreaped child and a starved thread apart."""
    running_process = run_background(["sleep", "30"], process_class=_UnstoppableRunningProcess)
    # The stop is what a starved reader thread looks like once it is in its read loop; stopping
    # while the thread is still setting that loop up reports its setup frame instead.
    wait_for(
        lambda: _READER_LOOP_FRAME in (running_process.get_reader_thread_stack() or ()),
        timeout=10.0,
        error_message="The reader thread never reached its read loop",
    )
    parent_connection, child_connection = Pipe()
    child_connection.close()
    with ConcurrencyGroup(name="test-warm-process-stop-timeout") as concurrency_group:
        warm_process = _WarmMngrProcess(
            connection=parent_connection, running_process=running_process, concurrency_group=concurrency_group
        )
        try:
            with capture_error_logs() as error_records:
                warm_process.terminate(is_after_call_timeout=True)
        finally:
            RunningProcess.terminate(running_process, force_kill_seconds=10.0)
    assert len(error_records) == 1
    record = error_records[0]
    assert "Timed out stopping a warm mngr process (after_call_timeout=True, join_wall_seconds=" in record
    assert "join_monotonic_seconds=" in record
    assert _READER_LOOP_FRAME in record
