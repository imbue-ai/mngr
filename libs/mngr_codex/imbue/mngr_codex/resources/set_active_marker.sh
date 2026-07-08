#!/usr/bin/env bash
# UserPromptSubmit hook: mark the agent RUNNING and record the turn's root.
#
# codex runs this before each user turn (see build_codex_hooks_config), passing
# a JSON payload on stdin that carries the session id and the rollout transcript
# path. It marks the root turn active and, when this prompt opens a fresh root
# turn, records the payload's session id as the turn's root in
# `codex_root_session` and the rollout `transcript_path` in
# `codex_transcript_path`, and clears any stranded `permissions_waiting` marker so
# the new turn does not inherit a prior (denied/cancelled) dialog's state.
#
# Async-subagent model: codex subagents run ASYNCHRONOUSLY, so the marker is not
# a simple touch/remove. It is recomputed under a lock from two pieces of state
# -- the `codex_root_active` flag this hook sets and the per-subagent files the
# SubagentStart/Stop hooks maintain -- so the `active` marker (RUNNING) stays
# present until the root turn AND every in-flight subagent are done. The shared
# helper codex_marker_state.sh owns the lock, the paths, and the recompute (see
# its header for the full invariant).
#
# Why record the root only when the marker is ABSENT: the root agent's
# UserPromptSubmit always fires while `active` is absent (it opens the turn),
# whereas a nested or recursive `codex` process sharing this CODEX_HOME (and thus
# these hooks) fires its prompt while the parent marker is already present.
# Guarding the capture behind "marker absent" therefore means a nested codex
# cannot steal the root session id, so clear_active_marker.sh's root-session
# guard keeps a nested codex's Stop from flipping the still-working root agent to
# WAITING. Re-recording at each fresh root turn (including after `codex resume`,
# which may assign a new session id) keeps the root + transcript path correct.
#
# Why also record the transcript path here: codex writes a single rollout JSONL
# per session and hands its absolute path to every hook as `transcript_path`.
# stream_transcript.sh tails exactly that file, so this hook is the single source
# of truth for which rollout to follow. The path can change across resume (codex
# may open a fresh rollout), so it is re-captured at each fresh root turn too.
#
# Submit signal: after the marker is set, this hook fires the
# `mngr-submit-<tmux session>` tmux wait-for channel that CodexAgent.send_message
# blocks on. Signalling AFTER the recompute means a caller that wakes on it always
# observes the agent as RUNNING, closing the race between submit and the lifecycle
# flip. Channel prefix is kept in sync with codex_config.py's
# SUBMIT_WAIT_CHANNEL_PREFIX (a test pins this).
#
# Marker / root / transcript-path file names are kept in sync with
# codex_config.py via the sourced helper. Never writes stdout (codex treats
# UserPromptSubmit stdout as additional model context); avoids `set -e` so a
# malformed payload can't disrupt codex's loop.

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "set_active_marker.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

payload=$(cat)

# shellcheck source=codex_marker_state.sh
. "$MNGR_AGENT_STATE_DIR/commands/codex_marker_state.sh"

codex_marker_lock

# Capture the root session id + rollout transcript path only at a fresh root turn
# (marker absent), checked under the lock so it reflects the pre-invocation
# state. A nested codex's prompt fires while the marker is present and so skips
# this block, leaving the root's recorded session/transcript intact.
if [ ! -e "$CODEX_MARKER_FILE" ]; then
    # session_id is a lowercase 8-4-4-4-12 hex UUID; codex_extract_field returns
    # whatever string value the key carries, so an empty result just means no
    # session id was present (the clear hook's liveness fallback then applies).
    session_id=$(codex_extract_field "session_id" "$payload")
    if [ -n "$session_id" ]; then
        printf '%s' "$session_id" > "$CODEX_ROOT_SESSION_FILE"
    fi

    transcript_path=$(codex_extract_field "transcript_path" "$payload")
    if [ -n "$transcript_path" ]; then
        printf '%s' "$transcript_path" > "$CODEX_TRANSCRIPT_PATH_FILE"
    fi

    # Clear any stranded permission-waiting marker as a new root turn opens. A
    # dialog that was denied/cancelled (no PostToolUse) can outlive its turn; the
    # Stop hook normally removes it, but if that was missed the marker would make
    # this fresh turn report PERMISSIONS once `active` is set below. Guarded by the
    # same "marker absent" check as the root-session capture, so a nested codex
    # (whose prompt fires while the marker is present) never clears the root's
    # dialog state.
    #
    # Limitation: this only fires at a FRESH root turn (marker absent). Cancelling
    # a dialog (Esc) interrupts the turn and codex 0.139.0 fires no Stop for it
    # (verified live), so the `active` marker is also stranded -- the next prompt
    # then finds the marker PRESENT and skips this block, leaving permissions_waiting
    # until the next turn's Stop clears both. The lifecycle state stays WAITING
    # throughout (correct); only the waiting_reason sub-field is briefly off. See
    # the README "Known limitation" note.
    rm -f "$CODEX_PERMISSIONS_WAITING_FILE"
fi

# Ensure the subagents dir exists (the SubagentStart/Stop hooks also create it,
# but seeding it here keeps the recompute's ls -A from racing on a missing dir),
# mark the root turn active, and recompute the marker.
mkdir -p "$CODEX_SUBAGENTS_DIR"
touch "$CODEX_ROOT_ACTIVE_FILE"
codex_marker_recompute

codex_marker_unlock

# Wake any `mngr message` waiter blocked on this turn's submission. The `active`
# marker is now present (recompute ran above), so a caller that woke on this signal
# observes the agent as RUNNING. Only signal when running inside tmux (codex's
# pane sets $TMUX, which also tells `display-message` and `wait-for` which server
# to use); a headless run has no waiter and no server, so skip it entirely. Channel
# prefix matches codex_config.py's SUBMIT_WAIT_CHANNEL_PREFIX. Best-effort so it
# can't disrupt codex's loop.
if [ -n "${TMUX:-}" ]; then
    tmux wait-for -S "mngr-submit-$(tmux display-message -p '#S' 2>/dev/null)" 2>/dev/null || true
fi
