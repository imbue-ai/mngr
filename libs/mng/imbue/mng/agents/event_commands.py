"""Shell command builders for emitting agent lifecycle events.

These functions generate shell commands that append structured JSONL events
to an agent's event log. The commands are designed to be used in Claude Code
hooks (or any other agent hook system) and rely on standard environment
variables: MNG_AGENT_STATE_DIR, MNG_AGENT_ID, MNG_AGENT_NAME.
"""

from imbue.imbue_common.pure import pure


@pure
def build_state_transition_command(from_state: str, to_state: str) -> str:
    """Build a shell command that appends an agent state transition event.

    The command writes a single JSONL line to
    $MNG_AGENT_STATE_DIR/events/mng_agents/events.jsonl with an
    AgentStateTransitionEvent-compatible schema.

    Requires MNG_AGENT_STATE_DIR, MNG_AGENT_ID, and MNG_AGENT_NAME
    to be set in the environment.
    """
    # Try /proc/sys/kernel/random/uuid (Linux), fall back to uuidgen (macOS).
    # The printf >> append is atomic under PIPE_BUF.
    return (
        '_MNG_TS=$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z");'
        ' _MNG_EID="evt-$(cat /proc/sys/kernel/random/uuid 2>/dev/null'
        " || uuidgen | tr '[:upper:]' '[:lower:]'"
        ' || echo "$$-$RANDOM-$RANDOM")";'
        ' mkdir -p "$MNG_AGENT_STATE_DIR/events/mng_agents";'
        " printf"
        ' \'{"timestamp":"%s","type":"agent_state_transition","event_id":"%s",'
        '"source":"mng_agents","agent_id":"%s","agent_name":"%s",'
        f'"from_state":"{from_state}","to_state":"{to_state}"}}\\n\''
        ' "$_MNG_TS" "$_MNG_EID" "$MNG_AGENT_ID" "$MNG_AGENT_NAME"'
        ' >> "$MNG_AGENT_STATE_DIR/events/mng_agents/events.jsonl"'
    )
