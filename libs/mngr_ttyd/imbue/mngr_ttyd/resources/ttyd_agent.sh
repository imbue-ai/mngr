#!/bin/bash
# Ttyd dispatch script for the agent terminal.
#
# Attaches to an agent's tmux session, allowing users to interact
# with the agent via a web browser.
#
# Invoked by the consolidated ttyd server when the URL contains ?arg=agent.
# Accepts an optional second URL argument naming a target agent (?arg=agent&arg=<name>).
# When provided, attaches to the session "${MNGR_PREFIX}<name>". When
# omitted, falls back to the ambient tmux session (i.e. the one ttyd itself
# is running in, which is the primary agent).
#
# The agent's primary window is targeted by name (MNGR_PRIMARY_WINDOW_NAME,
# config tmux.primary_window_name, default "agent") rather than the literal :0
# index, so attaching works regardless of the user's tmux base-index.

set -euo pipefail

_TARGET_AGENT="${1:-}"
if [ -n "$_TARGET_AGENT" ]; then
    _SESSION="${MNGR_PREFIX:-}${_TARGET_AGENT}"
else
    _SESSION=$(tmux display-message -p '#{session_name}')
fi
_WINDOW="${MNGR_PRIMARY_WINDOW_NAME:-agent}"
unset TMUX
# `=` is tmux's exact-match prefix; without it, attaching to a gone agent
# could silently land on a prefix-collision sibling.
exec tmux attach -t "=$_SESSION:$_WINDOW"
