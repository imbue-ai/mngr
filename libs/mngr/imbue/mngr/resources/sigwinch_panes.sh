#!/usr/bin/env bash
# sigwinch_panes.sh -- nudge an agent's tmux panes to repaint after a client attaches.
#
# Sends SIGWINCH to every pane's child processes across all windows in a session
# so the in-pane process (e.g. claude) re-queries its terminal size (TIOCGWINSZ)
# and redraws at the newly attached client's dimensions. Without this nudge a
# client that attaches at a different size than the session was created at can be
# left with a stale, unpainted frame.
#
# We signal the pane PID *and its children* (via pgrep -P) rather than relying on
# the SIGWINCH tmux itself sends on resize: the agent is typically a child of the
# pane shell and may not be the pane's foreground process group leader, so tmux's
# automatic signal does not reach it reliably.
#
# Invoked from a per-session tmux `client-attached` hook via `run-shell -b`, which
# runs it in the background so the attach is never blocked while we wait out the
# delay below (it lets the window settle at the new client size before we signal).
# Invoked directly (e.g. in tests) it runs synchronously; set the delay to 0 there.
#
# Usage: sigwinch_panes.sh <session_name> <primary_window_name>

set -uo pipefail

SESSION="${1:?session name required}"
PRIMARY_WINDOW="${2:?primary window name required}"

# Delay (seconds) before signaling, so the window has resized to the attaching
# client first; without it the agent may re-query the old size. Overridable via
# the environment so tests can disable the wait (the production default mirrors
# the historical post-attach nudge).
SIGWINCH_DELAY_SECONDS="${MNGR_SIGWINCH_DELAY_SECONDS:-3}"


_nudge_panes() {
    sleep "${SIGWINCH_DELAY_SECONDS}"

    # Skip pinned windows: window-size=manual means the window never resizes on
    # attach, so there is nothing to repaint and the deliberately-fixed dimensions
    # must be left untouched. Read it on the primary window (by name, so the guard
    # holds regardless of the user's tmux base-index). A missing value (empty)
    # fails open -- we proceed to signal.
    local window_size
    window_size="$(tmux show-options -t "=${SESSION}:${PRIMARY_WINDOW}" -wv window-size 2>/dev/null || true)"
    if [ "${window_size}" = manual ]; then
        return 0
    fi

    # Signal every pane's children in every window of the session. list-panes -s
    # does not honor the exact-match "=" prefix, so iterate windows and run a
    # per-window list-panes instead (see TmuxWindowTarget docstring).
    tmux list-windows -t "=${SESSION}" -F '#I' 2>/dev/null | while read -r window_index; do
        tmux list-panes -t "=${SESSION}:${window_index}" -F '#{pane_pid}' 2>/dev/null \
            | xargs -I{} sh -c 'kill -WINCH {} $(pgrep -P {})' 2>/dev/null || true
    done
}

# Run synchronously: the tmux client-attached hook invokes this via `run-shell -b`,
# which already backgrounds it relative to the attach, so no extra backgrounding is
# needed here (and direct/synchronous invocation keeps tests deterministic).
_nudge_panes
exit 0
