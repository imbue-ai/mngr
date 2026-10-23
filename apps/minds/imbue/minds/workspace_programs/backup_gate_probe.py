"""Report what would hold a mutating backup program back: busy chats and an in-flight backup tick.

The chats are the agents actively RUNNING, or WAITING with their chat busy per
the template's background-task CLI when it has one (agents sharing the
repo-root work_dir, excluding the ``main``-type services agent --
worktree/worker agents live elsewhere and never count). Reads ``--agent-id``
(the services agent, whose state dir holds the backup events log); answers on
one ``GATE_RESULT_MARKER`` line.
"""

from imbue.minds.workspace_programs.backup_protocol import GATE_RESULT_MARKER
from imbue.minds.workspace_programs.backup_support import is_backup_tick_in_flight
from imbue.minds.workspace_programs.backup_support import list_running_chats
from imbue.minds.workspace_programs.program_support import argv_value
from imbue.minds.workspace_programs.program_support import emit_marker_json


def main() -> None:
    agent_id = argv_value("--agent-id", "")
    result: dict[str, object] = {"schema": 1}
    gate, gate_error = list_running_chats()
    if gate is None:
        result["gate_error"] = gate_error
        result["running_chats"] = []
    else:
        result["running_chats"] = gate["chats"]
    result["backup_tick_in_flight"] = is_backup_tick_in_flight(agent_id)
    emit_marker_json(GATE_RESULT_MARKER, result)


if __name__ == "__main__":
    main()
