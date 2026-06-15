#!/usr/bin/env bash
# SubagentStop hook: deregister a finished subagent, then recompute the marker.
#
# codex runs this when a subagent (the spawn_agent multi-agent feature) finishes,
# passing a JSON payload on stdin that carries the subagent's `agent_id`. It
# removes that subagent's file under `codex_subagents/` and recomputes the
# `active` marker, so once the last subagent stops AND the root turn is done the
# marker clears (the agent reports WAITING).
#
# Async-subagent model: codex subagents run ASYNCHRONOUSLY, so this SubagentStop
# may arrive either before or after the root's Stop, with no ordering guarantee.
# The shared recompute makes the order irrelevant: whichever of (root Stop, last
# SubagentStop) happens last is the one that finally clears `active`. See
# codex_marker_state.sh for the invariant and the lock.
#
# Never writes stdout (codex can treat Stop-class hook stdout as control output);
# avoids `set -e` so a malformed payload can't disrupt codex's loop.

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "subagent_stopped.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

payload=$(cat)

# shellcheck source=codex_marker_state.sh
. "$MNGR_AGENT_STATE_DIR/commands/codex_marker_state.sh"

# agent_id is a lowercase 8-4-4-4-12 hex UUID; an empty result just means none
# was present, in which case there is nothing to deregister.
agent_id=$(codex_extract_field "agent_id" "$payload")

codex_marker_lock

if [ -n "$agent_id" ]; then
    rm -f "$CODEX_SUBAGENTS_DIR/$agent_id"
fi
codex_marker_recompute

codex_marker_unlock
