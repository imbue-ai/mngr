from imbue.imbue_common.pure import pure


@pure
def build_state_transition_command(from_state: str, to_state: str) -> str:
    """Build a shell command that appends an agent state transition event.

    The command writes a single JSONL line to
    $MNG_AGENT_STATE_DIR/events/mng/agents/events.jsonl with an
    AgentStateTransitionEvent-compatible schema.

    Requires MNG_AGENT_STATE_DIR, MNG_AGENT_ID, and MNG_AGENT_NAME
    to be set in the environment.
    """
    # Uses /dev/urandom for event ID generation, matching chat.sh's generate_event_id().
    # The printf >> append is atomic under PIPE_BUF.
    return (
        '_MNG_TS=$(date -u +"%Y-%m-%dT%H:%M:%S.%NZ");'
        ' _MNG_EID="evt-$(head -c 16 /dev/urandom | xxd -p)";'
        ' mkdir -p "$MNG_AGENT_STATE_DIR/events/mng/agents";'
        " printf"
        ' \'{"timestamp":"%s","type":"agent_state_transition","event_id":"%s",'
        '"source":"mng/agents","agent_id":"%s","agent_name":"%s",'
        f'"from_state":"{from_state}","to_state":"{to_state}"}}\\n\''
        ' "$_MNG_TS" "$_MNG_EID" "$MNG_AGENT_ID" "$MNG_AGENT_NAME"'
        ' >> "$MNG_AGENT_STATE_DIR/events/mng/agents/events.jsonl"'
    )
