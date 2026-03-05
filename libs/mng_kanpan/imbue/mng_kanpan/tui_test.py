import time
from collections.abc import Callable
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any

from urwid.event_loop.main_loop import MainLoop
from urwid.widget.text import Text

from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng_kanpan.data_types import AgentBoardEntry
from imbue.mng_kanpan.data_types import BoardSnapshot
from imbue.mng_kanpan.data_types import CheckStatus
from imbue.mng_kanpan.data_types import PrInfo
from imbue.mng_kanpan.data_types import PrState
from imbue.mng_kanpan.tui import REFRESH_INTERVAL_SECONDS
from imbue.mng_kanpan.tui import _KanpanState
from imbue.mng_kanpan.tui import _carry_forward_pr_data
from imbue.mng_kanpan.tui import _finish_refresh
from imbue.mng_kanpan.tui import _request_refresh
from imbue.mng_kanpan.tui import _start_local_refresh


class _AlarmRecord(SimpleNamespace):
    """Record of a set_alarm_in call."""

    delay: float
    callback: object
    user_data: object


class _TestableLoop(MainLoop):
    """MainLoop subclass that records alarm operations instead of registering with the event loop."""

    def __init__(self) -> None:
        super().__init__(Text(""))
        self.alarms: list[_AlarmRecord] = []
        self.removed_alarms: list[object] = []
        self._next_handle = 0

    def set_alarm_in(self, sec: float, callback: Callable[..., Any], user_data: Any = None) -> int:
        handle = self._next_handle
        self._next_handle += 1
        self.alarms.append(_AlarmRecord(delay=sec, callback=callback, user_data=user_data))
        return handle

    def remove_alarm(self, handle: object) -> bool:
        self.removed_alarms.append(handle)
        return True


class _FakeExecutor:
    """Executor whose submit() always returns a pre-built future."""

    def __init__(self, future: Future[BoardSnapshot]) -> None:
        self._future = future

    def submit(self, fn: object, *args: object, **kwargs: object) -> Future[BoardSnapshot]:
        return self._future


def _make_state(**overrides: Any) -> _KanpanState:
    """Build a _KanpanState with fake urwid widgets and sensible defaults."""
    defaults: dict[str, Any] = {
        "mng_ctx": SimpleNamespace(config=SimpleNamespace(plugins={})),
        "frame": SimpleNamespace(body=None),
        "footer_left_text": SimpleNamespace(set_text=lambda text: None),
        "footer_left_attr": SimpleNamespace(set_attr_map=lambda m: None),
        "footer_right": SimpleNamespace(set_text=lambda text: None),
    }
    defaults.update(overrides)
    return _KanpanState.model_construct(**defaults)


def test_request_refresh_starts_immediately_when_cooldown_expired() -> None:
    loop = _TestableLoop()
    pre_built_future: Future[BoardSnapshot] = Future()
    pre_built_future.set_result(BoardSnapshot(entries=(), fetch_time_seconds=0.1))
    executor = _FakeExecutor(pre_built_future)
    state = _make_state(
        last_refresh_time=time.monotonic() - 100,
        executor=executor,
    )

    _request_refresh(loop, state, cooldown_seconds=5.0)

    # _start_refresh should have been called, setting refresh_future
    assert state.refresh_future is pre_built_future


def test_request_refresh_defers_when_within_cooldown() -> None:
    loop = _TestableLoop()
    state = _make_state(last_refresh_time=time.monotonic())

    _request_refresh(loop, state, cooldown_seconds=60.0)

    # Should not have started a refresh
    assert state.refresh_future is None
    # Should have scheduled a deferred alarm
    assert state.deferred_refresh_alarm is not None
    # Find the deferred refresh alarm (last alarm set)
    assert len(loop.alarms) == 1
    delay = loop.alarms[0].delay
    assert 59.0 < delay <= 60.0


def test_request_refresh_replaces_deferred_with_sooner_alarm() -> None:
    """A manual refresh (short cooldown) should replace a pending auto refresh (long cooldown)."""
    loop = _TestableLoop()
    now = time.monotonic()
    state = _make_state(
        last_refresh_time=now - 2,
        deferred_refresh_alarm=999,
        deferred_refresh_fire_at=now + 58,
    )

    _request_refresh(loop, state, cooldown_seconds=5.0)

    # Old alarm should have been removed
    assert 999 in loop.removed_alarms
    # New deferred alarm should have been scheduled
    assert state.deferred_refresh_alarm is not None
    assert len(loop.alarms) == 1
    delay = loop.alarms[0].delay
    assert 2.0 < delay <= 3.0


def test_request_refresh_keeps_existing_if_sooner() -> None:
    """An auto refresh request should not replace a sooner pending manual refresh."""
    loop = _TestableLoop()
    now = time.monotonic()
    state = _make_state(
        last_refresh_time=now - 2,
        deferred_refresh_alarm=777,
        deferred_refresh_fire_at=now + 3,
    )

    _request_refresh(loop, state, cooldown_seconds=60.0)

    # No alarms should have been removed or added
    assert len(loop.removed_alarms) == 0
    assert len(loop.alarms) == 0
    assert state.deferred_refresh_alarm == 777


def test_request_refresh_noop_when_already_refreshing() -> None:
    loop = _TestableLoop()
    existing_future: Future[BoardSnapshot] = Future()
    state = _make_state(refresh_future=existing_future)

    _request_refresh(loop, state, cooldown_seconds=0.0)

    # refresh_future should be unchanged (no new refresh started)
    assert state.refresh_future is existing_future
    assert len(loop.alarms) == 0


def test_finish_refresh_schedules_normal_interval_on_success() -> None:
    loop = _TestableLoop()
    snapshot = BoardSnapshot(entries=(), fetch_time_seconds=1.0)
    future: Future[BoardSnapshot] = Future()
    future.set_result(snapshot)
    state = _make_state(refresh_future=future)

    _finish_refresh(loop, state)

    assert state.snapshot == snapshot
    assert state.refresh_future is None
    # Should schedule the next auto-refresh at the normal interval
    auto_refresh_alarms = [a for a in loop.alarms if a.delay == REFRESH_INTERVAL_SECONDS]
    assert len(auto_refresh_alarms) == 1


def test_finish_refresh_uses_auto_cooldown_on_failure() -> None:
    """After a failed refresh, the next refresh should be deferred by auto_refresh_cooldown_seconds."""
    loop = _TestableLoop()
    future: Future[BoardSnapshot] = Future()
    future.set_exception(RuntimeError("GitHub API error"))
    state = _make_state(
        refresh_future=future,
        auto_refresh_cooldown_seconds=30.0,
    )

    _finish_refresh(loop, state)

    assert state.refresh_future is None
    # Should have scheduled a deferred refresh (not a normal interval refresh)
    assert state.deferred_refresh_alarm is not None
    assert len(loop.alarms) == 1
    delay = loop.alarms[0].delay
    assert 29.0 < delay <= 30.0


# === _carry_forward_pr_data ===


def _make_entry(
    name: str,
    pr: PrInfo | None = None,
    create_pr_url: str | None = None,
) -> AgentBoardEntry:
    """Create a minimal AgentBoardEntry for testing."""
    return AgentBoardEntry(
        name=AgentName(name),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
        pr=pr,
        create_pr_url=create_pr_url,
    )


def _make_pr(number: int = 1, head_branch: str = "mng/test") -> PrInfo:
    """Create a minimal PrInfo for testing."""
    return PrInfo(
        number=number,
        title="PR " + str(number),
        state=PrState.OPEN,
        url=f"https://github.com/org/repo/pull/{number}",
        head_branch=head_branch,
        check_status=CheckStatus.PASSING,
        is_draft=False,
    )


def test_carry_forward_pr_data_copies_pr_from_old() -> None:
    """PR data from old snapshot should be carried forward to entries with pr=None."""
    pr = _make_pr(number=42)
    old = BoardSnapshot(
        entries=(_make_entry("agent-a", pr=pr, create_pr_url="https://example.com"),),
        fetch_time_seconds=1.0,
    )
    new = BoardSnapshot(
        entries=(_make_entry("agent-a"),),
        fetch_time_seconds=0.5,
    )

    result = _carry_forward_pr_data(new, old)

    assert result.entries[0].pr == pr
    assert result.entries[0].create_pr_url == "https://example.com"


def test_carry_forward_pr_data_no_old_snapshot() -> None:
    """When old snapshot is None, new snapshot is returned unchanged."""
    new = BoardSnapshot(
        entries=(_make_entry("agent-a"),),
        fetch_time_seconds=0.5,
    )

    result = _carry_forward_pr_data(new, None)

    assert result is new


def test_carry_forward_pr_data_new_agent_without_old_match() -> None:
    """A new agent not in the old snapshot keeps pr=None."""
    old = BoardSnapshot(entries=(), fetch_time_seconds=1.0)
    new = BoardSnapshot(
        entries=(_make_entry("agent-new"),),
        fetch_time_seconds=0.5,
    )

    result = _carry_forward_pr_data(new, old)

    assert result.entries[0].pr is None


def test_carry_forward_pr_data_does_not_overwrite_existing_pr() -> None:
    """If the new entry already has a PR, it should not be overwritten."""
    old_pr = _make_pr(number=1)
    new_pr = _make_pr(number=2)
    old = BoardSnapshot(
        entries=(_make_entry("agent-a", pr=old_pr),),
        fetch_time_seconds=1.0,
    )
    new = BoardSnapshot(
        entries=(_make_entry("agent-a", pr=new_pr),),
        fetch_time_seconds=0.5,
    )

    result = _carry_forward_pr_data(new, old)

    assert result.entries[0].pr == new_pr


# === local-only refresh ===


def test_local_refresh_does_not_reset_last_refresh_time() -> None:
    """A local-only refresh should not update last_refresh_time."""
    loop = _TestableLoop()
    snapshot = BoardSnapshot(entries=(), fetch_time_seconds=0.1)
    future: Future[BoardSnapshot] = Future()
    future.set_result(snapshot)
    original_time = 1000.0
    state = _make_state(
        refresh_future=future,
        refresh_is_local_only=True,
        last_refresh_time=original_time,
    )

    _finish_refresh(loop, state)

    assert state.last_refresh_time == original_time
    assert state.refresh_future is None
    assert state.refresh_is_local_only is False


def test_local_refresh_does_not_schedule_next_auto_refresh() -> None:
    """A local-only refresh should not schedule the next periodic auto-refresh."""
    loop = _TestableLoop()
    snapshot = BoardSnapshot(entries=(), fetch_time_seconds=0.1)
    future: Future[BoardSnapshot] = Future()
    future.set_result(snapshot)
    state = _make_state(
        refresh_future=future,
        refresh_is_local_only=True,
    )

    _finish_refresh(loop, state)

    # No auto-refresh alarm should have been scheduled
    auto_refresh_alarms = [a for a in loop.alarms if a.delay == REFRESH_INTERVAL_SECONDS]
    assert len(auto_refresh_alarms) == 0


def test_start_local_refresh_noop_when_already_refreshing() -> None:
    """_start_local_refresh should do nothing if a refresh is already in flight."""
    loop = _TestableLoop()
    existing_future: Future[BoardSnapshot] = Future()
    state = _make_state(refresh_future=existing_future)

    _start_local_refresh(loop, state)

    assert state.refresh_future is existing_future
    assert len(loop.alarms) == 0
