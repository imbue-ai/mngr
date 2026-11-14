"""Tests for the error-state reporters and the gate that dedupes them."""

from uuid import uuid4

from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.error_notifications import ACCOUNT_SIGNED_OUT_TITLE
from imbue.minds.desktop_client.error_notifications import AccountSignedOutReporter
from imbue.minds.desktop_client.error_notifications import ErrorNotificationGate
from imbue.minds.desktop_client.error_notifications import WORKSPACE_RECOVERY_FAILED_TITLE
from imbue.minds.desktop_client.error_notifications import WorkspaceRecoveryFailureReporter
from imbue.minds.desktop_client.system_interface_health import HostRecoveryKind
from imbue.minds.desktop_client.system_interface_health import RecoveryFailureCause
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import RecordingNotificationDispatcher
from imbue.minds.desktop_client.testing import make_notification_feed_for_test
from imbue.minds.desktop_client.ui_models import NotificationKind
from imbue.mngr.primitives import AgentId

_COOLDOWN_SECONDS = 600.0


class _SettableClock(MutableModel):
    """A clock the test moves by hand."""

    now: float = Field(default=1000.0, description="The current reading")

    def __call__(self) -> float:
        return self.now


def _make_gate(clock: _SettableClock) -> ErrorNotificationGate:
    return ErrorNotificationGate(cooldown_seconds=_COOLDOWN_SECONDS, get_now_seconds=clock)


def test_gate_admits_only_the_first_report_of_an_episode() -> None:
    gate = _make_gate(_SettableClock())
    key = f"episode-{uuid4().hex}"

    assert gate.admit_episode_start(key) is True
    assert gate.admit_episode_start(key) is False


def test_gate_does_not_readmit_an_ended_episode_within_the_cooldown() -> None:
    clock = _SettableClock()
    gate = _make_gate(clock)
    key = f"episode-{uuid4().hex}"
    assert gate.admit_episode_start(key) is True
    gate.end_episode(key)

    clock.now += _COOLDOWN_SECONDS - 1
    is_admitted_within_cooldown = gate.admit_episode_start(key)
    # The refused report still opened the episode, so a later report in it stays silent too.
    gate.end_episode(key)
    clock.now += 2
    is_admitted_after_cooldown = gate.admit_episode_start(key)

    assert is_admitted_within_cooldown is False
    assert is_admitted_after_cooldown is True


def test_gate_keeps_keys_independent() -> None:
    gate = _make_gate(_SettableClock())

    assert gate.admit_episode_start(f"episode-{uuid4().hex}") is True
    assert gate.admit_episode_start(f"episode-{uuid4().hex}") is True


def test_account_signed_out_reporter_appends_one_account_level_entry_and_banner() -> None:
    dispatcher = RecordingNotificationDispatcher(is_electron=True)
    feed = make_notification_feed_for_test(dispatcher=dispatcher, is_main_window_focused=False)
    reporter = AccountSignedOutReporter(notification_feed=feed)
    user_id = uuid4().hex
    email = f"{uuid4().hex}@example.com"

    reporter(user_id, email)

    entries = feed.reconcile((), {}).entries
    assert [(entry.kind, entry.title, entry.account_id, entry.workspace_agent_id) for entry in entries] == [
        (NotificationKind.SYSTEM_EVENT, ACCOUNT_SIGNED_OUT_TITLE, user_id, "")
    ]
    assert email in entries[0].body
    assert [(request.title, request.subtitle, request.url) for request in dispatcher.dispatched] == [
        (email, ACCOUNT_SIGNED_OUT_TITLE, f"/accounts?account={user_id}")
    ]


def test_account_signed_out_reporter_leaves_the_banner_to_the_toast_while_a_main_window_is_focused() -> None:
    dispatcher = RecordingNotificationDispatcher(is_electron=True)
    feed = make_notification_feed_for_test(dispatcher=dispatcher, is_main_window_focused=True)
    reporter = AccountSignedOutReporter(notification_feed=feed)

    reporter(uuid4().hex, f"{uuid4().hex}@example.com")

    assert len(feed.reconcile((), {}).entries) == 1
    assert dispatcher.dispatched == []


def _make_recovery_failure_setup(
    clock: _SettableClock,
) -> tuple[SystemInterfaceHealthTracker, RecordingNotificationDispatcher, AgentId]:
    dispatcher = RecordingNotificationDispatcher(is_electron=True)
    tracker = SystemInterfaceHealthTracker()
    tracker.add_on_change_callback(
        WorkspaceRecoveryFailureReporter(
            notification_feed=make_notification_feed_for_test(dispatcher=dispatcher, is_main_window_focused=False),
            backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
            gate=_make_gate(clock),
        )
    )
    return tracker, dispatcher, AgentId()


def _fail_recovery(tracker: SystemInterfaceHealthTracker, agent_id: AgentId) -> None:
    tracker.mark_stuck(agent_id)
    tracker.mark_recovering(agent_id, HostRecoveryKind.START)
    assert (
        tracker.mark_recovery_failed(agent_id, f"start failed {uuid4().hex}", RecoveryFailureCause.STEP_FAILED) is True
    )


def test_recovery_failure_reporter_notifies_once_per_outage_and_lands_on_the_workspace() -> None:
    tracker, dispatcher, agent_id = _make_recovery_failure_setup(_SettableClock())

    _fail_recovery(tracker, agent_id)
    # A retry that fails again inside the same outage re-fires RECOVERY_FAILED.
    tracker.mark_recovering(agent_id, HostRecoveryKind.RESTART)
    tracker.mark_recovery_failed(agent_id, f"restart failed {uuid4().hex}", RecoveryFailureCause.STEP_FAILED)

    assert [(request.subtitle, request.url) for request in dispatcher.dispatched] == [
        (WORKSPACE_RECOVERY_FAILED_TITLE, f"/workspace/{agent_id}")
    ]
    assert dispatcher.dispatched[0].title == str(agent_id)[:8]


def test_recovery_failure_reporter_stays_silent_for_stuck_and_recovering() -> None:
    tracker, dispatcher, agent_id = _make_recovery_failure_setup(_SettableClock())

    tracker.mark_stuck(agent_id)
    tracker.mark_recovering(agent_id, HostRecoveryKind.START)
    tracker.record_probe_success(agent_id)

    assert dispatcher.dispatched == []


def test_recovery_failure_reporter_notifies_a_new_outage_once_the_cooldown_has_passed() -> None:
    clock = _SettableClock()
    tracker, dispatcher, agent_id = _make_recovery_failure_setup(clock)

    _fail_recovery(tracker, agent_id)
    tracker.record_probe_success(agent_id)
    clock.now += _COOLDOWN_SECONDS / 2
    # A workspace that flaps back into failure soon after is not announced again.
    _fail_recovery(tracker, agent_id)
    tracker.record_probe_success(agent_id)
    clock.now += _COOLDOWN_SECONDS
    _fail_recovery(tracker, agent_id)

    assert len(dispatcher.dispatched) == 2
