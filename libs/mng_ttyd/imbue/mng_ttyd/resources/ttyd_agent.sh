#!/bin/bash
# Ttyd dispatch script for the agent terminal.
#
# Attaches to the primary role agent's tmux session, allowing users
# to interact with the agent via a web browser.
#
# Invoked by the consolidated ttyd server when the URL contains ?arg=agent.

set -euo pipefail

_SESSION=$(tmux display-message -p '#{session_name}')
unset TMUX
exec tmux attach -t "$_SESSION":0
