#!/usr/bin/env bash
# Minimal local reproduction of the minds workspace copy-paste / mouse-scroll issue.
#
# WHAT THIS REPRODUCES
# --------------------
# In a real minds workspace, ttyd serves a browser terminal that attaches to an
# agent's tmux *session* (a different session than ttyd itself lives in), and the
# forever-claude-template writes a `~/.tmux.conf` containing:
#
#     set -g alternate-screen off
#     set -g mouse on
#
# With `mouse on`, tmux grabs every mouse event, so ttyd's xterm.js front-end can
# no longer do native browser text selection / copy, and the mouse wheel scrolls
# tmux scrollback instead of the browser's. The goal of this script is to give a
# fast local loop for finding the combination of tmux options + ttyd client
# options where BOTH copy-paste AND mouse-wheel scroll work.
#
# This script mirrors the real topology: it starts a tmux server on a *dedicated
# socket* with a *dedicated session* (so it never touches your own tmux), seeds it
# with scrollable content, then runs ttyd whose command does `unset TMUX; tmux -L
# <socket> attach -t <session>` -- exactly like libs/mngr_ttyd/.../ttyd_agent.sh.
#
# USAGE
# -----
#   libs/mngr_ttyd/scripts/repro_ttyd_tmux_copy_paste.sh
#
# Then open the printed http://127.0.0.1:<PORT> URL in a browser and try to:
#   1. drag-select some of the numbered lines and copy them (Ctrl/Cmd-C), and
#   2. scroll up/down with the mouse wheel.
#
# Iterate by overriding the knobs below via environment variables, e.g.:
#   MOUSE=off ./repro_ttyd_tmux_copy_paste.sh          # native selection + scroll, no tmux mouse
#   TTYD_OPTS='disableLeaveAlert=true rendererType=webgl' ./repro_...sh
#   MOUSE=on ALTSCREEN=off ./repro_...sh               # the default: reproduces the bug
#
# Press Ctrl-C in this terminal to tear everything down.

set -euo pipefail

# --- Tunable knobs (override via env) ---------------------------------------
# tmux `mouse` setting. `on` reproduces the bug; `off` restores browser-native
# selection and wheel scroll (at the cost of mouse support inside TUIs).
MOUSE="${MOUSE:-on}"
# tmux `alternate-screen` setting, matching the forever-claude-template default.
ALTSCREEN="${ALTSCREEN:-off}"
# Space-separated list of ttyd `-t key=value` client options. These map to
# xterm.js terminal options plus a few ttyd extras (disableLeaveAlert,
# disableResizeOverlay, rendererType, macOptionIsMeta, etc.). This is the main
# lever for the browser side of the copy-paste / scroll behavior.
TTYD_OPTS="${TTYD_OPTS:-disableLeaveAlert=true}"
# Optional custom ttyd index.html (served via ttyd -I). The stock ttyd 1.7.7
# binary ships an xterm.js with no OSC 52 handler, so OSC52=on is a no-op against
# it. Point this at a self-contained index built from ttyd's `main` branch (which
# loads @xterm/addon-clipboard) to give the stock binary an OSC 52-capable client
# -- the wire protocol is unchanged, so the old binary + new client interoperate.
# Then OSC52=on + MOUSE=on copies a plain drag straight to the system clipboard
# while tmux keeps the mouse wheel for scrollback. Build it with:
#   git clone https://github.com/tsl0922/ttyd ~/project/ttyd
#   (cd ~/project/ttyd/html && corepack enable && yarn install && yarn build)
#   TTYD_INDEX=~/project/ttyd/html/dist/inline.html OSC52=on ./repro_...sh
TTYD_INDEX="${TTYD_INDEX:-}"
# Port ttyd listens on (use 0 for a random port).
PORT="${PORT:-7681}"
# Dedicated tmux socket + session names so we never collide with a real tmux.
SOCKET="${SOCKET:-mngr_repro}"
SESSION="${SESSION:-mngr-agent}"
# How many numbered lines to seed so wheel scroll has something to scroll.
SCROLLBACK_LINE_COUNT="${SCROLLBACK_LINE_COUNT:-300}"
# Candidate fix #1 (DOES NOT WORK with ttyd 1.7.7): keep `mouse on` but route
# copy-mode selections to the system clipboard via OSC 52. ttyd 1.7.7's bundled
# xterm.js registers NO OSC 52 handler and loads no clipboard addon (verified by
# inspecting its served bundle), so these escapes are silently dropped by the
# browser. Left here as a knob for testing against a future ttyd that does
# support inbound OSC 52. Default off because it is a no-op here.
OSC52="${OSC52:-off}"
# Candidate fix #2 (WORKS with ttyd 1.7.7): control whether the tmux *client*
# switches the browser terminal into its alternate screen on attach. By default
# tmux emits the alt-screen enter sequence, which empties xterm.js's own
# scrollback -- so the only way to scroll is tmux's mouse-driven copy-mode
# (which needs `mouse on`, which in turn breaks native selection/copy). Set
# CLIENT_ALTSCREEN=off to neuter smcup/rmcup so tmux draws on the MAIN screen;
# then xterm.js keeps native scrollback and, with MOUSE=off, the browser handles
# BOTH drag-select copy AND wheel scroll natively. Recommended fix combination:
#   MOUSE=off CLIENT_ALTSCREEN=off
CLIENT_ALTSCREEN="${CLIENT_ALTSCREEN:-on}"

# --- Preconditions ----------------------------------------------------------
for _tool in tmux ttyd; do
    if ! command -v "$_tool" >/dev/null 2>&1; then
        echo "error: '$_tool' is not installed or not on PATH" >&2
        exit 1
    fi
done

# --- Cleanup ----------------------------------------------------------------
# Kill any prior repro server on this socket, both now and on exit.
_kill_tmux_server() {
    tmux -L "$SOCKET" kill-server >/dev/null 2>&1 || true
}
_kill_tmux_server
trap _kill_tmux_server EXIT INT TERM

# --- Build the isolated tmux config -----------------------------------------
# We write a dedicated config file and pass it with `-f` rather than touching
# ~/.tmux.conf, but the contents mirror what the forever-claude-template writes.
_CONF_FILE="$(mktemp -t mngr_repro_tmux_conf.XXXXXX)"
trap 'rm -f "$_CONF_FILE"; _kill_tmux_server' EXIT INT TERM
cat >"$_CONF_FILE" <<EOF
# Auto-generated repro config -- mirrors forever-claude-template/.mngr/settings.toml
set -g alternate-screen $ALTSCREEN
set -g mouse $MOUSE
EOF

# Candidate fix: keep mouse on, but make a plain drag copy to the system
# clipboard via OSC 52. `terminal-features ...:clipboard` makes tmux believe the
# outer terminal can take an OSC 52 clipboard write (otherwise tmux suppresses
# it unless the terminfo advertises the `Ms` capability); `set-clipboard on`
# tells tmux to actually emit it; and rebinding MouseDragEnd1Pane to
# copy-pipe-and-cancel performs the copy (which, with the above, also emits OSC
# 52) on drag-release in both emacs- and vi-style copy-mode.
if [ "$OSC52" = "on" ]; then
    cat >>"$_CONF_FILE" <<'EOF'
set -as terminal-features ',*:clipboard'
set -g set-clipboard on
bind -T copy-mode    MouseDragEnd1Pane send -X copy-pipe-and-cancel
bind -T copy-mode-vi MouseDragEnd1Pane send -X copy-pipe-and-cancel
EOF
fi

# Candidate fix #2: stop the tmux client from using the browser terminal's
# alternate screen, so xterm.js retains native scrollback for the mouse wheel.
if [ "$CLIENT_ALTSCREEN" = "off" ]; then
    cat >>"$_CONF_FILE" <<'EOF'
set -ga terminal-overrides ',*:smcup@:rmcup@'
EOF
fi

# --- Seed the inner session with scrollable content -------------------------
# The seeded window prints SCROLLBACK_LINE_COUNT numbered lines (so there is
# scrollback to test the wheel against) and then drops into an interactive shell
# (so you can also test selection of a live prompt).
_seed_command="seq 1 ${SCROLLBACK_LINE_COUNT}; echo; echo '--- repro ready: drag-select to copy, wheel to scroll ---'; exec bash"

tmux -L "$SOCKET" -f "$_CONF_FILE" new-session -d -s "$SESSION" -x 200 -y 50 \
    "$_seed_command"

# --- Report what we're running ----------------------------------------------
echo "================ ttyd + tmux copy-paste repro ================"
echo "  open in browser : http://127.0.0.1:$PORT"
echo "                    (with PORT=0, see ttyd's startup line for the actual port)"
echo "  tmux socket   : $SOCKET   (separate from your default tmux)"
echo "  tmux session  : $SESSION"
echo "  mouse         : $MOUSE        (override with MOUSE=on|off)"
echo "  alternate-screen: $ALTSCREEN     (override with ALTSCREEN=on|off)"
echo "  ttyd options  : $TTYD_OPTS"
echo "                  (override with TTYD_OPTS='k=v k2=v2')"
echo "  osc52 copy    : $OSC52       (override with OSC52=on|off)"
echo "  client altscreen: $CLIENT_ALTSCREEN     (override with CLIENT_ALTSCREEN=on|off)"
if [ -n "$TTYD_INDEX" ]; then
    echo "  custom client : $TTYD_INDEX"
else
    echo "  custom client : (none -- stock ttyd client; OSC52 will be a no-op on 1.7.7)"
fi
echo "--------------------------------------------------------------"
echo "  Open in browser, then test BOTH:"
if [ "$MOUSE" = "off" ]; then
    echo "    1) drag-select text (NO Shift) + Ctrl/Cmd-C -> native browser copy"
elif [ "$OSC52" = "on" ]; then
    echo "    1) drag-select text (NO Shift) and release -> should copy to clipboard"
else
    echo "    1) drag-select text + Ctrl/Cmd-C to copy"
    echo "       (with mouse=on you may need to hold Shift while dragging)"
fi
if [ "$CLIENT_ALTSCREEN" = "off" ]; then
    echo "    2) mouse-wheel up/down -> native browser scrollback"
else
    echo "    2) mouse-wheel up/down to scroll (tmux copy-mode; needs mouse=on)"
fi
echo "  Ctrl-C here to tear down."
echo "=============================================================="

# --- Assemble the ttyd client-option flags ----------------------------------
_ttyd_opt_flags=()
for _opt in $TTYD_OPTS; do
    _ttyd_opt_flags+=(-t "$_opt")
done

# Serve a custom OSC 52-capable client instead of the stock one when requested.
_ttyd_index_flags=()
if [ -n "$TTYD_INDEX" ]; then
    if [ ! -f "$TTYD_INDEX" ]; then
        echo "error: TTYD_INDEX file not found: $TTYD_INDEX" >&2
        exit 1
    fi
    _ttyd_index_flags+=(-I "$TTYD_INDEX")
fi

# --- Run ttyd ---------------------------------------------------------------
# The command mirrors libs/mngr_ttyd/imbue/mngr_ttyd/resources/ttyd_agent.sh:
# clear any inherited $TMUX and attach to the dedicated socket+session. `=` is
# tmux's exact-match prefix so we never land on a prefix-collision sibling.
#
# ttyd runs in the foreground (no `exec`) on purpose: keeping this shell alive
# means its EXIT/INT/TERM trap fires when ttyd exits (e.g. on Ctrl-C), tearing
# down the tmux server and removing the temp config file. `exec`-ing ttyd would
# replace this shell and the trap would never run, leaking both.
ttyd -W -p "$PORT" "${_ttyd_index_flags[@]}" "${_ttyd_opt_flags[@]}" \
    bash -c "unset TMUX; exec tmux -L $(printf %q "$SOCKET") attach -t $(printf %q "=$SESSION")"
