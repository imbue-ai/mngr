#!/usr/bin/env bash
# SubagentStart hook: register an in-flight subagent so the marker stays RUNNING.
#
# codex runs this when a subagent (the spawn_agent multi-agent feature) starts,
# passing a JSON payload on stdin that carries the subagent's `agent_id`. It
# records one empty file per live subagent under `codex_subagents/`, keyed by
# agent_id, so the shared recompute keeps the `active` marker (RUNNING) present
# while any subagent is in flight.
#
# Why this hook exists: codex subagents run ASYNCHRONOUSLY. The root agent's Stop
# fires when the root model loop is done WHILE its subagents may still be running,
# and their SubagentStop hooks arrive later with no ordering guarantee. Tracking
# each live subagent here -- and removing it in subagent_stopped.sh -- lets the
# marker stay present until the root turn AND every subagent are done, instead of
# flipping to WAITING the moment the root loop ends. See codex_marker_state.sh for
# the invariant and the lock.
#
# Never writes stdout (codex can treat Stop-class hook stdout as control output);
# avoids `set -e` so a malformed payload can't disrupt codex's loop.

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "subagent_started.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

payload=$(cat)

# shellcheck source=codex_marker_state.sh
. "$MNGR_AGENT_STATE_DIR/commands/codex_marker_state.sh"

# agent_id is a lowercase 8-4-4-4-12 hex UUID; an empty result just means none
# was present, in which case there is nothing to register.
agent_id=$(codex_extract_field "agent_id" "$payload")

codex_marker_lock

mkdir -p "$CODEX_SUBAGENTS_DIR"
if [ -n "$agent_id" ]; then
    touch "$CODEX_SUBAGENTS_DIR/$agent_id"
fi
codex_marker_recompute

codex_marker_unlock
