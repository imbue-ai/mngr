#!/usr/bin/env bash
# sigwinch_panes.sh -- keep an agent's tmux pane usable and repainted after a client attaches.
#
# Runs from a per-session tmux `client-attached` hook (via `run-shell -b`) in one of two modes:
#
#   fit   -- the default sizing policy. The agent window is pinned to window-size=manual (so a
#            degenerate client on the shared tmux server can never collapse it), and this script
#            re-fits the window to the attaching client's geometry, floored at a usable minimum
#            (_MIN_WIDTH x _MIN_HEIGHT) so a degenerate (e.g. 2x1) client cannot shrink the pane
#            below what Claude Code's Ink TUI needs. It then signals the panes to repaint.
#            Usage: sigwinch_panes.sh <session> <primary_window> fit <client_width> <client_height>
#
#   nudge -- an explicit window_size policy is in effect; tmux owns resizing. This script only
#            repaints, and skips a truly-pinned (window-size=manual) window (nothing to repaint).
#            Usage: sigwinch_panes.sh <session> <primary_window> nudge
#
# Repaint = send SIGWINCH to every pane's child processes (via pgrep -P) across all windows so the
# in-pane process (e.g. claude) re-queries TIOCGWINSZ and redraws. We signal the pane PID *and its
# children* rather than relying on tmux's automatic SIGWINCH: the agent is typically a child of the
# pane shell and may not be the pane's foreground process-group leader, so tmux's signal does not
# reach it reliably.
#
# `run-shell -b` backgrounds this relative to the attach, so the attach is never blocked while we
# wait out the delay below (it lets the window settle at the new client first). Invoked directly
# (e.g. in tests) it runs synchronously; set the delay to 0 there.

set -uo pipefail

SESSION="${1:?session name required}"
PRIMARY_WINDOW="${2:?primary window name required}"
MODE="${3:-nudge}"
CLIENT_WIDTH="${4:-}"
CLIENT_HEIGHT="${5:-}"

# Minimum usable geometry for the agent pane. A real terminal is honored exactly; anything smaller
# (in either dimension) -- notably a degenerate 2x1 ttyd/web-shell client -- is floored to this so
# Claude Code's TUI can still render and marker-based `mngr message` delivery works.
_MIN_WIDTH=80
_MIN_HEIGHT=24

# Delay (seconds) before acting, so the window has settled to the attaching client first; without
# it the agent may re-query the old size. Overridable via the environment so tests can disable it.
SIGWINCH_DELAY_SECONDS="${MNGR_SIGWINCH_DELAY_SECONDS:-3}"


# Echo a positive integer, or _MIN if the input is not a positive integer or is below _MIN.
_floored() {
    local value="$1" minimum="$2"
    case "${value}" in
        '' | *[!0-9]*) echo "${minimum}"; return 0 ;;
    esac
    if [ "${value}" -lt "${minimum}" ]; then
        echo "${minimum}"
    else
        echo "${value}"
    fi
}

# Current size ("<width> <height>") of the session's most-recently-active attached client, or
# empty when none is attached. Read fresh from tmux rather than trusting the hook-fire arguments:
# a resize burst (e.g. a sash drag in the web terminal, or a hidden tab growing to full size)
# fires many overlapping hook instances, and with captured-at-fire geometry the last resize-window
# to land can be a stale intermediate size -- which a manual-pinned window then keeps until the
# next client event. Reading at act time makes every instance converge on the real current size.
_current_client_size() {
    tmux list-clients -t "=${SESSION}" -F '#{client_activity} #{client_width} #{client_height}' 2>/dev/null \
        | sort -rn | awk 'NR==1 {print $2, $3}'
}

# Re-fit the manual-pinned agent window to the client, floored at the usable minimum. Prefers the
# live client size (see _current_client_size); falls back to the hook-fire arguments when no
# client is listed (defensive -- client-attached fires with the client already attached).
_fit_window() {
    local width height current
    current="$(_current_client_size)"
    if [ -n "${current}" ]; then
        width="${current% *}"
        height="${current#* }"
    else
        width="${CLIENT_WIDTH}"
        height="${CLIENT_HEIGHT}"
    fi
    width="$(_floored "${width}" "${_MIN_WIDTH}")"
    height="$(_floored "${height}" "${_MIN_HEIGHT}")"
    tmux resize-window -t "=${SESSION}:${PRIMARY_WINDOW}" -x "${width}" -y "${height}" 2>/dev/null || true
}

# Send SIGWINCH to every pane's children in every window of the session. list-panes -s does not
# honor the exact-match "=" prefix, so iterate windows and run a per-window list-panes instead.
_signal_panes() {
    tmux list-windows -t "=${SESSION}" -F '#I' 2>/dev/null | while read -r window_index; do
        tmux list-panes -t "=${SESSION}:${window_index}" -F '#{pane_pid}' 2>/dev/null \
            | xargs -I{} sh -c 'kill -WINCH {} $(pgrep -P {})' 2>/dev/null || true
    done
}

_main() {
    if [ "${MODE}" = fit ]; then
        # We own the (manual-pinned) window: fit it to the client immediately so a live attach or
        # terminal resize is tracked promptly (matching tmux's native continuous "latest"), then
        # settle and fit AGAIN before repainting -- the post-settle fit re-reads the client size,
        # so whichever overlapping hook instance acts last still leaves the window matching the
        # client as it is by then (a resize burst otherwise ends on whichever instance's
        # resize-window happened to land last).
        _fit_window
        sleep "${SIGWINCH_DELAY_SECONDS}"
        _fit_window
        _signal_panes
        return 0
    fi

    sleep "${SIGWINCH_DELAY_SECONDS}"

    # nudge mode: tmux owns resizing. Skip a truly-pinned window -- window-size=manual means it
    # never resizes on attach, so there is nothing to repaint and its fixed dimensions must be
    # left untouched. A missing value (empty) fails open -- we proceed to signal.
    local window_size
    window_size="$(tmux show-options -t "=${SESSION}:${PRIMARY_WINDOW}" -wv window-size 2>/dev/null || true)"
    if [ "${window_size}" = manual ]; then
        return 0
    fi
    _signal_panes
}

_main
exit 0
