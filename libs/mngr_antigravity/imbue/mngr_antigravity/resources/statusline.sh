#!/usr/bin/env bash
# statusLine command: the single source of truth for agy agent lifecycle.
#
# agy invokes the configured statusLine command on every agent-state change,
# piping a JSON payload on stdin (verified live against agy 1.0.6/1.0.7). The
# payload carries `agent_state` (observed vocabulary: initializing,
# authenticating, idle, working), `conversation_id` (always the ROOT
# conversation, even while a subagent runs), plus model/context_window/vcs/etc.
# Crucially, top-level `agent_state` already aggregates subagent activity: it
# stays `working` continuously while a subagent runs and returns to `idle` only
# once root + subagents are all done (75 consecutive `working` samples spanning
# a ~29s subagent run, zero mid-turn `idle` blips). That makes a single
# `agent_state` check a correct, self-contained signal for whole-turn busy/idle
# state -- no per-conversation bookkeeping needed.
#
# On each invocation this script:
#   1. Parses `agent_state` and `conversation_id` (POSIX grep/sed only -- no jq;
#      jq may be absent on remote hosts).
#   2. Records the (root) `conversation_id` in `root_conversation` when present
#      -- the only consumer is the resume prelude in assemble_command.
#   3. Maintains the `active` marker BaseAgent reads for RUNNING/WAITING: active
#      iff `agent_state` is NOT in {idle, initializing, authenticating, ""}
#      (a denylist, so any present/future busy state counts as RUNNING; `idle`
#      is the canonical done state).
#   4. When busy, fires the tmux wait-for submission signal `mngr message` waits
#      on. Firing on every busy sample (not just the idle->working edge) means a
#      message queued while the agent is already busy is also confirmed; a signal
#      with no registered waiter is a harmless no-op.
#   5. Renders the status row (stdout IS the rendered statusline, unlike the hook
#      scripts whose stdout agy treats as injected steps). mngr is lifecycle-only
#      and prints NOTHING of its own -- agy already shows working/idle -- so the
#      row stays exactly as it would be without mngr. When the user configured
#      their own statusLine, its command (recorded by the provisioner) is run with
#      the same payload and ONLY its output is emitted, preserving it verbatim.
#
# Marker / root-file names are kept in sync with antigravity_config.py. Avoids
# `set -e` so a malformed payload can't disrupt agy's loop.

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "statusline.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

marker_file="$MNGR_AGENT_STATE_DIR/active"
root_file="$MNGR_AGENT_STATE_DIR/root_conversation"

payload=$(cat)

# Parse the agent_state string value (e.g. "working", "idle").
agent_state=$(
    printf '%s' "$payload" \
        | grep -oE '"agent_state"[[:space:]]*:[[:space:]]*"[^"]*"' \
        | head -n 1 \
        | sed -E 's/.*:[[:space:]]*"([^"]*)".*/\1/'
)

# Parse the (root) conversation id -- a full UUID.
conv_id=$(
    printf '%s' "$payload" \
        | grep -oE '"conversation_id"[[:space:]]*:[[:space:]]*"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"' \
        | head -n 1 \
        | sed -E 's/.*"([0-9a-f-]+)".*/\1/'
)

# Record the root conversation whenever the payload carries one. agy always
# reports the root id here (never a subagent's), so recording it unconditionally
# keeps `root_conversation` pointed at the true root for resume.
if [ -n "$conv_id" ]; then
    printf '%s' "$conv_id" > "$root_file"
fi

# Shared common-transcript helpers: mngr_common_transcript_flush forces a
# synchronous pass of the raw streamer + common-transcript converter so the
# WAITING signal can't outrun them (see below and the shared library header).
# Sourced defensively (no set -e here): the lib is provisioned by
# Host._ensure_shared_shell_libs, but a missing lib must never disrupt agy's
# statusLine loop.
if [ -r "$MNGR_AGENT_STATE_DIR/commands/mngr_common_transcript_lib.sh" ]; then
    # shellcheck source=../../../mngr/imbue/mngr/resources/mngr_common_transcript_lib.sh
    source "$MNGR_AGENT_STATE_DIR/commands/mngr_common_transcript_lib.sh"
fi

# Busy = any state that is not a known not-working state. Denylist so any
# current/future busy state (working, ...) keeps the agent RUNNING.
case "$agent_state" in
    idle | initializing | authenticating | "")
        # Flush the transcript pipeline so a consumer that harvests the final
        # message from the common transcript on the WAITING transition (e.g.
        # Catalyst's mngr_runner) can't outrun the converter. Only on the
        # busy->idle edge (marker still present from a prior busy sample), not
        # on every idle/startup sample: that bounds the synchronous conversion
        # to once per turn, right as the turn ends.
        if [ -e "$marker_file" ] && command -v mngr_common_transcript_flush >/dev/null 2>&1; then
            mngr_common_transcript_flush
        fi
        rm -f "$marker_file"
        ;;
    *)
        touch "$marker_file"
        # Confirm message submission: agy enters a busy state once it starts
        # processing an enqueued prompt. `mngr message` registers a waiter on
        # this channel before sending Enter; the signal wakes it. Only fire when
        # actually inside a tmux session (agy always is): the `#S` session name
        # must match the waiter's channel, and gating on TMUX keeps the script a
        # pure no-op when run outside tmux. A signal with no waiter is harmless.
        if [ -n "${TMUX:-}" ]; then
            tmux wait-for -S "mngr-submit-$(tmux display-message -p '#S')" 2>/dev/null || true
        fi
        ;;
esac

# Render the statusline. mngr's statusLine is lifecycle-only: the side-effects
# above (marker, root, submit signal) are the whole point, and agy already shows
# working/idle in its own UI, so mngr prints NOTHING of its own -- the status row
# stays exactly as the user would see it without mngr. When the user configured
# their own statusLine, we preserve it: agy allows only one statusLine command
# (which must be mngr's, for lifecycle), so we run the user's command here -- with
# the same payload on stdin, exactly as agy would deliver it (recorded by the
# provisioner; see USER_STATUSLINE_COMMAND_FILENAME) -- and emit ONLY its output.
# Guarded so a missing, empty, or failing user command can never break the row or
# the side-effects above.
user_cmd_file="$MNGR_AGENT_STATE_DIR/user_statusline_command"
if [ -s "$user_cmd_file" ]; then
    printf '%s' "$payload" | bash -c "$(cat "$user_cmd_file")" 2>/dev/null || true
fi
