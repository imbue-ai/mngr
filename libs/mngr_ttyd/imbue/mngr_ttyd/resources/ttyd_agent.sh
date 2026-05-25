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

set -euo pipefail

_TARGET_AGENT="${1:-}"
if [ -n "$_TARGET_AGENT" ]; then
    _SESSION="${MNGR_PREFIX:-}${_TARGET_AGENT}"
else
    _SESSION=$(tmux display-message -p '#{session_name}')
fi
unset TMUX
# The leading `=` forces tmux exact-session matching. Without it, tmux falls back
# to session-name prefix matching, so attaching to an agent whose session is gone
# could silently land on a sibling session whose name shares the requested name
# as a prefix (matches TmuxSessionTarget.as_shell_arg() on the Python side).
exec tmux attach -t "=$_SESSION:0"
