#!/usr/bin/env bash
#
# events_demo.sh - Runs the commands necessary to demonstrate how a mind can react to external events, eg, from a slack message
#

set -euo pipefail

# make a new random ID for this agent (before even starting, just for my sanity)
export AGENT_ID=agent-`python3 -c "from uuid import uuid4; print(uuid4().hex)"`

# create the mind
( source .env && export CLAUDE_CODE_DISABLE_FAST_MODE=1 && mkdir ~/.minds/$AGENT_ID && cd ~/.minds/$AGENT_ID && mkdir -p thinking/.mng && cp ~/agent_repos/elena-code/instructions.txt ./instructions.txt && cp ~/agent_repos/elena-code/thinking/.mng/settings.toml ./thinking/.mng/settings.toml && mng create selene --agent-id $AGENT_ID --no-connect --await-ready --agent-type claude-mind --env ROLE=thinking --label mind=true --yes --pass-env ANTHROPIC_API_KEY --in-place -- --dangerously-skip-permissions && echo " " && echo "http://127.0.0.1:8420/agents/$AGENT_ID/" && echo " " )

# start a tmux session and attach to the chat via "mng chat"
tmux new-session -s event_demo
# within there:
mng chat
# select the slack notification chat, nothing in there

# detach

# then connect to the mind's thinking terminal
mng connect selene

# detach

# stick an event into the correct location:
mkdir -p "${HOME}/.mng/agents/${AGENT_ID}/events/slack" && echo '{"timestamp":"2026-03-09T17:44:17.140593000Z","type":"message_fetched","event_id":"evt-ca85d93dd8cb473399f21c57855676a5","source":"messages","channel_id":"C0AKG4CDMFC","channel_name":"temp-testing","message_ts":"1773077894.879889","raw":{"user":"UT6BRCJU8","type":"message","ts":"1773077894.879889","client_msg_id":"d90eacc6-e683-4fcf-ba33-e0b4feae6379","text":"This is an !IMPORTANT! message that I want to be notified about (to make sure notifications work)","team":"TSTHRQ7MY","blocks":[{"type":"rich_text","block_id":"noFKv","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"This is an !IMPORTANT! message that I want to be notified about (to make sure notifications work)"}]}]}]}}' > "${HOME}/.mng/agents/${AGENT_ID}/events/slack/events.jsonl"

# reconnect to the thinking terminal and watch it process the event
mng connect selene

# detach

# re-attach to the chat session:
tmux attach -t event_demo

# hurray, there is an event!
