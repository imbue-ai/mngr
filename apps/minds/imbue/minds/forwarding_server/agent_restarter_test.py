from imbue.minds.forwarding_server.agent_restarter import AgentRestarter
from imbue.mngr.primitives import AgentId


def test_try_restart_debounces_within_cooldown() -> None:
    """A second restart attempt for the same agent within the cooldown period is skipped."""
    restarter = AgentRestarter(mngr_binary="true", cooldown_seconds=100.0)
    agent_id = AgentId()

    # First call should record the attempt
    restarter.try_restart(agent_id)

    # Capture the recorded time
    with restarter._lock:
        first_attempt = restarter._last_attempt_by_agent[str(agent_id)]

    # Second call within cooldown should not update the timestamp
    restarter.try_restart(agent_id)

    with restarter._lock:
        second_attempt = restarter._last_attempt_by_agent[str(agent_id)]

    assert first_attempt == second_attempt


def test_try_restart_allows_after_cooldown_expires() -> None:
    """A restart attempt is allowed once the cooldown period has elapsed."""
    restarter = AgentRestarter(mngr_binary="true", cooldown_seconds=0.0)
    agent_id = AgentId()

    restarter.try_restart(agent_id)

    with restarter._lock:
        first_attempt = restarter._last_attempt_by_agent[str(agent_id)]

    # Manually expire the cooldown by backdating the last attempt
    with restarter._lock:
        restarter._last_attempt_by_agent[str(agent_id)] = first_attempt - 1.0

    restarter.try_restart(agent_id)

    with restarter._lock:
        second_attempt = restarter._last_attempt_by_agent[str(agent_id)]

    assert second_attempt > first_attempt


def test_try_restart_tracks_agents_independently() -> None:
    """Restart cooldowns are tracked per agent, not globally."""
    restarter = AgentRestarter(mngr_binary="true", cooldown_seconds=100.0)
    agent_a = AgentId()
    agent_b = AgentId()

    restarter.try_restart(agent_a)
    restarter.try_restart(agent_b)

    with restarter._lock:
        assert str(agent_a) in restarter._last_attempt_by_agent
        assert str(agent_b) in restarter._last_attempt_by_agent
