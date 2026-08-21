"""Unit tests for the channel's per-workspace health frame (``_ui_health_message``)."""

from imbue.minds.desktop_client.app import _ui_health_message
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.mngr.primitives import AgentId


def test_the_no_op_start_reaches_the_frame_only_on_the_terminal_state() -> None:
    """A start that booted no host is news only once the episode has ended badly.

    Mid-episode the start may yet report, and a frame that carried the flag
    early would badge a machine "Not responding" while the app is still trying.
    The restart error is gated the same way and for the same reason, so both
    ride the terminal frame together.
    """
    tracker = SystemInterfaceHealthTracker()
    agent_id = AgentId.generate()
    tracker.mark_restarting(agent_id, start_only=True)
    tracker.record_restart_started_nothing(agent_id)

    in_progress = _ui_health_message(tracker, str(agent_id), AgentHealth.RESTARTING)
    assert in_progress.is_restart_a_no_op is False
    assert in_progress.error is None
    # The shape of the restart, however, is news precisely while one is running.
    assert in_progress.is_restart_start_only is True

    tracker.mark_restart_failed(agent_id, "The system interface did not respond.")
    terminal = _ui_health_message(tracker, str(agent_id), AgentHealth.RESTART_FAILED)
    assert terminal.is_restart_a_no_op is True
    assert terminal.error == "The system interface did not respond."
    # And stops being news once the restart is over: a held reading would go on
    # describing a restart that is no longer running.
    assert terminal.is_restart_start_only is None


def test_a_restart_that_really_booted_a_host_keeps_the_restart_framing() -> None:
    """Nothing recorded is the honest default: without a report there is no evidence either way."""
    tracker = SystemInterfaceHealthTracker()
    agent_id = AgentId.generate()
    tracker.mark_restarting(agent_id, start_only=False)
    tracker.mark_restart_failed(agent_id, "The system interface did not respond.")

    assert _ui_health_message(tracker, str(agent_id), AgentHealth.RESTART_FAILED).is_restart_a_no_op is False


def test_the_frame_reports_a_user_bounce_so_the_machines_list_can_name_it() -> None:
    """The full stop+start the user clicked must reach the list, not just the card.

    The recovery-info route the card polls already reports this; the machines
    list has only the health frame, so without it the row understates the user's
    own restart as a reconnect while the card they opened calls it a restart.
    """
    tracker = SystemInterfaceHealthTracker()
    agent_id = AgentId.generate()
    tracker.mark_restarting(agent_id, start_only=False)

    assert _ui_health_message(tracker, str(agent_id), AgentHealth.RESTARTING).is_restart_start_only is False


def test_a_workspace_with_no_restart_running_describes_none() -> None:
    """A machine that is merely stuck has no restart shape to report."""
    tracker = SystemInterfaceHealthTracker()
    agent_id = AgentId.generate()
    tracker.mark_stuck(agent_id)

    assert _ui_health_message(tracker, str(agent_id), AgentHealth.STUCK).is_restart_start_only is None
