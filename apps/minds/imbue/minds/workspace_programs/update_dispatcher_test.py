from imbue.minds.workspace_programs.update_dispatcher import _is_agent_busy


def test_a_working_agent_is_busy_whatever_its_type() -> None:
    """A chat working past its turn, or one whose type mngr's config does not name while its process
    runs, must hold the update watchdog off; nothing else about it needs to be busy."""
    no_busy_chats = (frozenset(), frozenset(), frozenset())
    for state in ("RUNNING", "RUNNING_BACKGROUND", "RUNNING_UNKNOWN_AGENT_TYPE"):
        assert _is_agent_busy({"name": "chat", "state": state}, no_busy_chats), state
    for state in ("WAITING", "UNKNOWN", "REPLACED", "DONE", "STOPPED"):
        assert not _is_agent_busy({"name": "chat", "state": state}, no_busy_chats), state
