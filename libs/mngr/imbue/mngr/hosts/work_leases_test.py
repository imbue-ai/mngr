import subprocess
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.hosts.work_leases import WORK_LEASE_DIR_NAME
from imbue.mngr.hosts.work_leases import build_lifecycle_probe_read_command
from imbue.mngr.hosts.work_leases import determine_agent_probe_result
from imbue.mngr.hosts.work_leases import parse_lifecycle_probe_reading
from imbue.mngr.hosts.work_leases import parse_work_leases
from imbue.mngr.hosts.work_leases import resolve_live_work_leases
from imbue.mngr.hosts.work_leases import select_live_work_leases
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import LifecycleProbeReading
from imbue.mngr.primitives import LifecycleProbeResult
from imbue.mngr.primitives import WorkLease

_LEASE_NOW: Final[datetime] = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)

# Enough empty leases to outnumber the descriptors the scan is given, so one held open per
# empty lease would starve the real lease that sorts after them.
_EMPTY_LEASE_COUNT: Final[int] = 60
_SCAN_FILE_DESCRIPTOR_LIMIT: Final[int] = 40


def test_parse_lifecycle_probe_reading_splits_the_turn_from_the_leases() -> None:
    reading = parse_lifecycle_probe_reading(
        "ACTIVE=true\nLEASE=bg-1\tuntil:1767355200\nLEASE=wake-2\tuntil:1767355260\n"
    )

    assert reading == LifecycleProbeReading(
        is_active=True,
        raw_leases="bg-1\tuntil:1767355200\nwake-2\tuntil:1767355260",
    )


@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param("ACTIVE=\n", id="marker_absent"),
        pytest.param("ACTIVE=false\n", id="marker_absent_as_the_listing_spells_it"),
        pytest.param("", id="nothing_read"),
        pytest.param("awk: cannot open /agents/a/leases/*\n", id="only_noise"),
    ],
)
def test_parse_lifecycle_probe_reading_reads_no_turn_and_no_lease(stdout: str) -> None:
    """Anything the read did not label is not a marker and not a lease."""
    assert parse_lifecycle_probe_reading(stdout) == LifecycleProbeReading()


def test_build_probe_read_command_reads_the_marker_and_the_leases_of_one_agent() -> None:
    """One call collects both halves, and survives a lease read that exits non-zero."""
    command = build_lifecycle_probe_read_command("/state/agents/a")

    assert "/state/agents/a/active" in command
    assert "/state/agents/a/leases/*" in command
    assert command.endswith("; :")


def test_build_probe_read_command_quotes_a_directory_that_needs_it() -> None:
    command = build_lifecycle_probe_read_command("/state/with a space")

    assert "'/state/with a space'/active" in command


def test_build_probe_read_command_reads_a_lease_that_sorts_after_many_empty_ones(tmp_path: Path) -> None:
    """A lease the scan opens but reads nothing from must still be closed.

    An empty lease yields no line, so a scan that only closes what it read spends a descriptor
    on each one and then cannot open the leases that follow -- which reads as the work having
    ended. Run against a lowered descriptor limit so a host's worth of leases fits in a test.
    """
    lease_dir = tmp_path / WORK_LEASE_DIR_NAME
    lease_dir.mkdir()
    for index in range(_EMPTY_LEASE_COUNT):
        (lease_dir / f"empty-{index}").touch()
    (lease_dir / "zz-task").write_text("until:1767358800\n")

    command = f"ulimit -n {_SCAN_FILE_DESCRIPTOR_LIMIT}; {build_lifecycle_probe_read_command(str(tmp_path))}"
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=30)

    reading = parse_lifecycle_probe_reading(result.stdout)
    assert [lease.name for lease in parse_work_leases(reading.raw_leases)] == ["zz-task"], result.stderr


def test_parse_work_leases_reads_the_bound_whatever_else_shares_the_line() -> None:
    leases = parse_work_leases("bg-1\tuntil:1767355200\nwake-2\tsince:1767355100 until:1767355200\n")

    assert [lease.name for lease in leases] == ["bg-1", "wake-2"]
    assert leases[0].expires_at == datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert leases[1].expires_at == datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_work_leases_ignores_a_token_it_does_not_know() -> None:
    """The grammar can grow: a reader that has already shipped must not choke on a new key."""
    leases = parse_work_leases("bg-1\tuntil:1767355200 reason:compaction\n")

    assert [lease.name for lease in leases] == ["bg-1"]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("bg-1\n", id="no_bound"),
        pytest.param("bg-1\t\n", id="empty_bound"),
        pytest.param("bg-1\tforever\n", id="unknown_bound"),
        pytest.param("bg-1\tsince:1767355100\n", id="unbounded_with_a_token_the_grammar_ignores"),
        pytest.param("bg-1\tpid:4821\n", id="a_token_the_grammar_no_longer_bounds_by"),
        pytest.param("\tuntil:1767355200\n", id="no_name"),
        pytest.param("bad name\tuntil:1767355200\n", id="name_outside_the_grammar"),
        # `str.isdigit` admits these; `int` does not, and a raising parse would take the
        # whole probe -- or, in the batched path, the whole listing -- down with it.
        pytest.param("bg-1\tuntil:²\n", id="superscript_deadline"),
        pytest.param("bg-1\tuntil:99999999999999999999\n", id="deadline_past_the_end_of_time"),
    ],
)
def test_parse_work_leases_drops_anything_without_a_usable_bound(raw: str) -> None:
    assert parse_work_leases(raw) == ()


def test_work_lease_refuses_to_be_unbounded() -> None:
    with pytest.raises(ValueError):
        WorkLease(name="bg-1")


def test_work_lease_refuses_to_be_unnamed() -> None:
    """An unnamed lease could never be matched to the work it stands for."""
    with pytest.raises(ValueError):
        WorkLease(name="", expires_at=_LEASE_NOW)


def test_work_lease_refuses_a_name_outside_the_grammar() -> None:
    """A tab in a name would corrupt every other agent's leases in a batched read."""
    with pytest.raises(ValueError):
        WorkLease(name="bg\t1", expires_at=_LEASE_NOW)


def test_select_live_work_leases_drops_a_lease_whose_deadline_has_passed() -> None:
    leases = (
        WorkLease(name="unexpired", expires_at=_LEASE_NOW + timedelta(seconds=30)),
        WorkLease(name="expired", expires_at=_LEASE_NOW - timedelta(seconds=30)),
    )

    assert [lease.name for lease in select_live_work_leases(leases, _LEASE_NOW)] == ["unexpired"]


def test_resolve_live_work_leases_reads_and_bounds_in_one_step() -> None:
    raw = "wake-2\tuntil:1767355260\nstale-3\tuntil:1767355140\n"

    assert [lease.name for lease in resolve_live_work_leases(raw, _LEASE_NOW)] == ["wake-2"]


def _probe(
    reading: LifecycleProbeReading,
    is_blocked_on_dialog: bool = False,
    tmux_info: str | None = "0|claude|123",
    ps_output: str | None = "",
) -> LifecycleProbeResult:
    return determine_agent_probe_result(
        tmux_info=tmux_info,
        is_blocked_on_dialog=is_blocked_on_dialog,
        expected_process_name="claude",
        ps_output=ps_output,
        reading=reading,
        now=_LEASE_NOW,
    )


def test_a_turn_that_ended_leaving_work_outstanding_is_running_background() -> None:
    """The state the turn marker alone cannot express: no turn, but something will wake the agent."""
    probe = _probe(LifecycleProbeReading(is_active=False, raw_leases="task-1\tuntil:1767358800"))

    assert probe.state == AgentLifecycleState.RUNNING_BACKGROUND
    assert [lease.name for lease in probe.leases] == ["task-1"]


def test_a_background_working_probe_carries_the_pid_the_process_table_gave_it() -> None:
    """Promoting the state must not cost the pid: an exit watcher is opened on that field.

    ``AgentObserver`` reads a live agent with no pid as "the process table was not there",
    and so as no evidence the watched process exited. A promotion that dropped the pid
    would close the watcher of an agent whose work is still in flight.
    """
    probe = _probe(
        LifecycleProbeReading(is_active=False, raw_leases="task-1\tuntil:1767358800"),
        ps_output="123 1 claude\n",
    )

    assert probe.state == AgentLifecycleState.RUNNING_BACKGROUND
    assert probe.pid == 123


def test_a_live_agent_whose_type_is_unknown_keeps_the_state_the_marker_gave_it() -> None:
    """Its type is absent from this config, not its hooks from its state dir.

    A claude agent whose plugin was disabled keeps writing the turn marker and keeps leasing
    its work, and neither changes what mngr can say about a process it cannot name: the leases
    are read only where the state is WAITING, so this one is reported as it was read.
    """
    probe = determine_agent_probe_result(
        tmux_info="0|python3|123",
        is_blocked_on_dialog=False,
        expected_process_name="claude",
        ps_output="",
        reading=LifecycleProbeReading(is_active=True, raw_leases="task-1\tuntil:1767358800"),
        now=_LEASE_NOW,
        is_agent_type_known=False,
    )

    assert probe.state == AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE
    assert probe.leases == ()


def test_an_open_turn_is_running_whatever_it_left_outstanding() -> None:
    """A turn in progress is the nearer fact: the agent is working now, not merely about to."""
    probe = _probe(LifecycleProbeReading(is_active=True, raw_leases="task-1\tuntil:1767358800"))

    assert probe.state == AgentLifecycleState.RUNNING


def test_a_dialog_outranks_work_the_agent_left_running() -> None:
    """Holding for a person is the one thing that means the user is the next mover.

    The leases go unreported with it: the field is published as what makes a state RUNNING_BACKGROUND,
    so a reader may take one as saying the agent will move itself.
    """
    reading = LifecycleProbeReading(is_active=True, raw_leases="task-1\tuntil:1767358800")

    probe = _probe(reading, is_blocked_on_dialog=True)

    assert probe.state == AgentLifecycleState.WAITING
    assert probe.leases == ()


def test_a_turn_that_ended_with_nothing_outstanding_is_waiting() -> None:
    probe = _probe(LifecycleProbeReading(is_active=False))

    assert probe.state == AgentLifecycleState.WAITING
    assert probe.leases == ()


def test_a_lease_whose_bound_has_lapsed_does_not_hold_the_agent() -> None:
    """Work that died without cleaning up stops counting on its own."""
    probe = _probe(LifecycleProbeReading(is_active=False, raw_leases="task-1\tuntil:1767355140"))

    assert probe.state == AgentLifecycleState.WAITING
    assert probe.leases == ()


def test_a_deadline_bound_lease_holds_without_a_process_table() -> None:
    """A deadline says something will wake the agent whatever the host can report."""
    probe = _probe(LifecycleProbeReading(is_active=False, raw_leases="task-1\tuntil:1767358800"), ps_output=None)

    assert probe.state == AgentLifecycleState.RUNNING_BACKGROUND


@pytest.mark.parametrize(
    ("tmux_info", "expected_state"),
    [
        pytest.param(None, AgentLifecycleState.STOPPED, id="no_session"),
        pytest.param("1|bash|4242", AgentLifecycleState.DONE, id="pane_process_exited"),
    ],
)
def test_a_lease_cannot_revive_an_agent_that_is_not_live(
    tmux_info: str | None, expected_state: AgentLifecycleState
) -> None:
    """Whatever its directory still holds, an agent whose process is gone is carrying nothing."""
    probe = _probe(
        LifecycleProbeReading(is_active=False, raw_leases="task-1\tuntil:1767358800"),
        tmux_info=tmux_info,
    )

    assert probe.state == expected_state
    assert probe.leases == ()
