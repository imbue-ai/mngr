#!/usr/bin/env bash
# statusLine command: the single source of truth for agy agent lifecycle.
#
# agy invokes the configured statusLine command on every agent-state change,
# piping a JSON payload on stdin (verified live against agy 1.0.6/1.0.7). The
# payload carries `agent_state` (observed vocabulary: initializing,
# authenticating, idle, working), `conversation_id` (always the ROOT
# conversation, even while a subagent runs), plus model/context_window/vcs/etc.
# It also carries `task_count`, agy's count of background tasks, left out at
# zero, and `subagents`, each with a `status` of `running` or `completed`; agy
# can end the root's turn while a subagent still runs.
#
# On each invocation this script:
#   1. Parses `agent_state` and `conversation_id` (POSIX grep/sed only -- no jq;
#      jq may be absent on remote hosts).
#   2. Records the (root) `conversation_id` in `root_conversation` when present,
#      and touches `antigravity_ready` once `agent_state` is past startup.
#   3. Maintains the `active` marker BaseAgent reads for RUNNING/WAITING: active
#      iff `agent_state` is NOT in {idle, initializing, authenticating, ""}
#      (a denylist, so any present/future busy state counts as RUNNING; `idle`
#      is the canonical done state). Around it, rewrites the background task
#      records, one `agy-task-<n>` per counted task and one `agy-subagent-<n>`
#      per running subagent: before removing `active`, after creating it.
#   4. When busy, fires the tmux wait-for submission signal that OLDER mngr
#      versions wait on. FIXME: remove the signal once released senders no
#      longer wait on it. Current mngr confirms sends by polling the `active`
#      marker itself and never listens on the channel -- tmux LATCHES a signal
#      fired with no waiter (it is NOT a harmless no-op: the next waiter is
#      woken instantly), which is exactly why the channel stopped being
#      trusted. Kept only so old senders messaging a newly created agent still
#      confirm.
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
ready_file="$MNGR_AGENT_STATE_DIR/antigravity_ready"
root_file="$MNGR_AGENT_STATE_DIR/root_conversation"
model_state_file="$MNGR_AGENT_STATE_DIR/model_state.json"

payload=$(cat)

# Parse the agent_state string value (e.g. "working", "idle").
agent_state=$(
    printf '%s' "$payload" \
        | grep -oE '"agent_state"[[:space:]]*:[[:space:]]*"[^"]*"' \
        | head -n 1 \
        | sed -E 's/.*:[[:space:]]*"([^"]*)".*/\1/'
)

# Parse agy's count of background tasks.
task_count=$(
    printf '%s' "$payload" \
        | grep -oE '"task_count"[[:space:]]*:[[:space:]]*[0-9]+' \
        | head -n 1 \
        | sed -E 's/.*:[[:space:]]*//'
)

# Count the running subagents, matching each string whole so text inside one cannot end the list.
running_subagent_count=$(
    printf '%s' "$payload" \
        | grep -oE '"subagents"[[:space:]]*:[[:space:]]*\[("([^"\\]|\\.)*"|[^]"])*\]' \
        | head -n 1 \
        | grep -oE '"status"[[:space:]]*:[[:space:]]*"running"' \
        | wc -l
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
    printf '%s' "$conv_id" > "$root_file.$$.tmp" && mv -f "$root_file.$$.tmp" "$root_file"
fi

# Parse the model agy reports and mirror it to the uniform model_state.json the workspace's
# model bar reads. VERIFIED against a live agy 1.4.7 payload, which carries an OBJECT:
#
#   "model":{"id":"Gemini 3.8 Flash (High)","display_name":"...","effort":"high"}
#
# so `id` is a DISPLAY NAME, not agy's `gemini-3.8-flash-high` slug, and it carries the effort
# tier as a parenthesised suffix. A model whose effort cannot be adjusted reports no `effort`,
# and its name is written as reported.
# For one that can, the effort is written as its own axis and its suffix is dropped from the
# name, so the model reads the same at every effort ("Gemini 3.8 Flash").
#
# Two passes because there is no jq here: isolate the model object (it has no nested braces),
# then take its fields. Falls back to a bare string value, so an agy that ever reports
# "model":"..." still works. Tolerant throughout: no match writes nothing and the bar shows no
# model, which must never disturb the lifecycle work above.
agy_effort=""
agy_model_object=$(
    printf '%s' "$payload" \
        | grep -oE '"model"[[:space:]]*:[[:space:]]*\{[^}]*\}' \
        | head -n 1
)
if [ -n "$agy_model_object" ]; then
    agy_model=$(
        printf '%s' "$agy_model_object" \
            | grep -oE '"id"[[:space:]]*:[[:space:]]*"[^"]*"' \
            | head -n 1 \
            | sed -E 's/.*:[[:space:]]*"([^"]*)".*/\1/'
    )
    agy_effort=$(
        printf '%s' "$agy_model_object" \
            | grep -oE '"effort"[[:space:]]*:[[:space:]]*"[^"]*"' \
            | head -n 1 \
            | sed -E 's/.*:[[:space:]]*"([^"]*)".*/\1/'
    )
else
    agy_model=$(
        printf '%s' "$payload" \
            | grep -oE '"model"[[:space:]]*:[[:space:]]*"[^"]*"' \
            | head -n 1 \
            | sed -E 's/.*:[[:space:]]*"([^"]*)".*/\1/'
    )
fi
if [ -n "$agy_model" ] && [ -n "$agy_effort" ]; then
    # agy title-cases the tier in the name ("high" -> " (High)"), so compare case-insensitively.
    effort_suffix=$(printf ' (%s)' "$agy_effort" | tr '[:upper:]' '[:lower:]')
    model_tail=$(printf '%s' "${agy_model: -${#effort_suffix}}" | tr '[:upper:]' '[:lower:]')
    if [ "$model_tail" = "$effort_suffix" ]; then
        agy_model="${agy_model:0:$((${#agy_model} - ${#effort_suffix}))}"
    fi
fi
if [ -n "$agy_model" ]; then
    if [ -n "$agy_effort" ]; then
        printf -v model_state_json '{"model": "%s", "effort": "%s"}' "$agy_model" "$agy_effort"
    else
        printf -v model_state_json '{"model": "%s"}' "$agy_model"
    fi
    printf '%s' "$model_state_json" > "$model_state_file.tmp" 2>/dev/null \
        && mv -f "$model_state_file.tmp" "$model_state_file" 2>/dev/null
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
if [ -r "$MNGR_AGENT_STATE_DIR/commands/mngr_background_task_lib.sh" ]; then
    # shellcheck source=../../../mngr/imbue/mngr/resources/mngr_background_task_lib.sh
    source "$MNGR_AGENT_STATE_DIR/commands/mngr_background_task_lib.sh"
fi

# _rewrite_background_tasks <prefix> <count> <kind> <native_type>: write <prefix>1 to <prefix><count>, then remove every other <prefix> record.
_rewrite_background_tasks() {
    local prefix="$1" count="$2" kind="$3" native_type="$4" index record_path
    for ((index = 1; index <= count; index++)); do
        mngr_background_task_write "$prefix$index" "$kind" "$native_type"
    done
    for record_path in "$(mngr_background_task_dir)/$prefix"*; do
        [ -e "$record_path" ] || continue
        index="${record_path##*/"$prefix"}"
        case "$index" in
            '' | *[!0-9]* | 0*) ;;
            *) [ "$index" -gt "$count" ] || continue ;;
        esac
        rm -f "$record_path"
    done
}

# Rewrite the records from what this payload reports running, unless it could not be read.
# agy gives no task type, so a record's native type is the name of the payload field it came from.
_record_background_tasks() {
    [ -n "$agent_state" ] && command -v mngr_background_task_write >/dev/null 2>&1 || return 0
    _rewrite_background_tasks agy-task- "$((10#${task_count:-0}))" SHELL task_count
    _rewrite_background_tasks agy-subagent- "$((running_subagent_count))" SUBAGENT subagents
}

# Marks agy past startup, which `mngr upgrade` waits for after a restart.
case "$agent_state" in
    initializing | authenticating | "") ;;
    *) touch "$ready_file" ;;
esac

# Busy = any state that is not a known not-working state. Denylist so any
# current/future busy state (working, ...) keeps the agent RUNNING.
case "$agent_state" in
    idle | initializing | authenticating | "")
        _record_background_tasks
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
        _record_background_tasks
        # Submission signal for OLDER mngr senders only: current mngr confirms
        # sends by polling the `active` marker touched above and never waits on
        # this channel (see the header FIXME for why the signal is scheduled
        # for removal). Only fire when actually inside a tmux session (agy
        # always is): the `#S` session name must match an old waiter's channel,
        # and gating on TMUX keeps the script a pure no-op outside tmux.
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
