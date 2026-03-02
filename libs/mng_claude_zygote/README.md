# Design for "Changelings"

This plugin implements the core functionality for claude-based "changelings": LLM-based agents that can have multiple conversation threads, react to events, and store persistent memories.

The core idea is to have a "primary" agent that serves as the "inner monologue" of the changeling, and that reacts to events (like new messages in conversation threads, scheduled events, sub-agent state changes, etc.)
Rather than having a direct conversation with the agent, the agent has a prompt that tells it what to do in response to various events, and it simply processes those until it decides that everything is complete and it can go to sleep (until it is awoken to process the next event)

In practice, "changelings" are just special mng agents that inherit from the ClaudeZygoteAgent

They can be thought of as a sort of "higher level" agent that is made by assembling a few different LLM-based programs

Fundamentally, changelings are mng agents where:
- The primary agent (from mng's perspective) is a claude code instance *that reacts to "events"* and forms the sort of "inner dialog" of the agent
- Users do *not* chat directly with this main "inner monolouge" agent--instead, they have conversational threads via the "llm live-chat" command-line tool, and those conversations are *exposed* to the primary agent by sending it events to react to.
- The core "inner monologue" agent can be thought of as reacting to events. It is sent messages (by a watcher, via "mng message") whenever new events appear in the event streams. For example:
    - `logs/messages/events.jsonl`: new conversation messages synced from the llm database (we'll need to filter out any where the source was the primary agent, since it obviously should not be notified for its own messages)
    - `logs/scheduled/events.jsonl`: one of the time based triggers happens ("mng schedule" can be used, via a skill, to schedule custom events at certain times, which in turn append the json data for that event)
    - `logs/mng_agents/events.jsonl`: a sub agent (launched by this primary agent) transitions to "waiting" (happens via our own hooks--goes through a modal hook like snapshot_and_save if remote, otherwise can just create directly)
    - `logs/stop/events.jsonl`: the primary agent tries to stop (for the first time--before thought complete, roughly). This allows it to do a last check of whether there is anything else worth responding to before going to sleep (and considering when it ought to wake)
    - `logs/monitor/events.jsonl`: (future) a local monitor agent appends a message/reminder to its file
- The primary agent is generally instructed to do everything via "mng" (because then all sub agents and work is visible / totally transparent to you)

## Event log structure

All event data uses a consistent append-only JSONL format stored under `<agent-data-dir>/logs/<source>/events.jsonl`. Every event line has a standard envelope:

    {"timestamp": "...", "type": "...", "event_id": "...", "source": "<source>", ...additional fields}

Event sources:
- `logs/conversations/events.jsonl` - conversation lifecycle events (created, model changed). Each event includes `conversation_id` and `model`.
- `logs/messages/events.jsonl` - all conversation messages across all conversations. Each event includes `conversation_id`, `role`, and `content`.
- `logs/scheduled/events.jsonl`: Each event corresponds to a scheduled trigger that the primary agent should react to. The event data includes the name of the trigger and any relevant metadata.
- `logs/mng_agents/events.jsonl`: all relevant agent state transitions (eg, when they become blocked, crash, finish, etc). Each event includes the agent_id, the new state, and any relevant metadata about the transition (eg, error message if it crashed)
- `logs/stop/events.jsonl`: for detecting when this agent tried to stop the first time
- `logs/monitor/events.jsonl`: (future) for injecting metacognitive thoughts or reminders from a local monitor agent
- `logs/claude_transcript/events.jsonl` - inner monologue transcript (written by Claude background tasks, not this plugin).

Every event is self-describing: you never need to know the filename to understand the event. The file organization is a performance/convenience choice, not a correctness one.

## Implementation details

- The "conversation threads" or "chat threads" are simply conversation ids that are tracked by the "llm" tool (a 3rd party CLI tool that is really nice and simple--we've made a plugin for it, llm-live-chat, that enables the below "llm live-chat" and "llm inject" commands)
- Users create new (and resume existing) conversations by calling a little "chat" command. It's just a little bash script that creates event json entries and also makes calls to "llm" so that users and agents don't need to remember the exact invocations. "chat --new" for a new chat and "chat --resume <conversation_id>" to resume. "chat" with no arguments lists all current conversation ids
- Agents create new conversations by using their "new chat" skill, which calls "chat --new --as-agent" and passing in the message as well
- Whenever the user (or the agent) creates a new conversation, the "chat" wrapper appends a `conversation_created` event to `logs/conversations/events.jsonl` (with the standard envelope plus `conversation_id` and `model`). The conversation is started by calling "llm live-chat" (for user messages) or "llm inject" (for agent messages)
- The ClaudeZygoteAgent runs a conversation watcher script in a tmux window that watches the llm database and, whenever it changes, syncs new messages to `logs/messages/events.jsonl` (with the standard envelope plus `conversation_id`, `role`, `content`)
- Thus the URL to view an existing chat conversation is simply done via a special ttyd server that runs the correct llm invocation: "llm live-chat --show-history -c --cid <conversation_id> -m <chat-model>" where chat-model comes from the most recent event in `logs/conversations/events.jsonl` with that conversation_id
- To list all conversations for this agent, we read `logs/conversations/events.jsonl` (append-only, last value per conversation_id wins)
- When invoking "llm live-chat", we pass in two tools: one for gathering context (recent messages from other conversations, inner monologue, recent events) and another for extra context (mng agent list, deeper history)
- A simple event watcher observes the event streams (`logs/messages/events.jsonl`, `logs/scheduled/events.jsonl`, `logs/mng_agents/events.jsonl`, `logs/stop/events.jsonl`) for changes, and when modified, sends the next unhandled event(s) to the primary agent (via "mng message")
- Changeling agents are assumed to run from a specially structured git repo that contains various skills, configuration, prompt files, and the code for any tools they have constructed for themselves. The layout is:
    - `GLOBAL.md` - shared instructions for all agents (symlinked as `CLAUDE.md` so Claude Code discovers it)
    - `settings.json` - shared Claude Code settings (symlinked as `.claude/settings.json`)
    - `thinking/PROMPT.md` - primary/inner monologue agent prompt (symlinked as `CLAUDE.local.md`)
    - `thinking/settings.json` - primary agent Claude Code settings (symlinked as `.claude/settings.local.json`)
    - `thinking/skills/` - skills for the thinking agent (symlinked as `.claude/skills`)
    - `talking/PROMPT.md` - talking agent prompt (future)
    - `talking/settings.json` - talking agent settings (future)
    - `talking/skills/` - skills for the talking agent (future)
    - `memory/` - shared memory directory (symlinked into Claude project memory)
- The `GLOBAL.md` serves as the core system prompt that is *shared* among all agents (the primary agent, any claude subagent it makes, and even any other agents created via mng with this repo as the target). It is symlinked to `CLAUDE.md` at the project root so Claude Code picks it up.
- The primary agent prompt is stored as `thinking/PROMPT.md` and symlinked as `CLAUDE.local.md` at the project root. Similarly, `thinking/settings.json` is symlinked as `.claude/settings.local.json`, and `thinking/skills/` is symlinked as `.claude/skills`. All of this is handled by the ClaudeZygoteAgent during provisioning.
- Other agent roles (e.g., `talking/`) can be defined by creating corresponding directories with their own `PROMPT.md`, `settings.json`, and `skills/` subdirectories (and creating an appropriate agent type for them in `.mng/settings.toml`)
- The prompts for the primary agent (both before shutdown and upon message receipt) should encourage it to keep track of messages that it received (via its own task list)
- *All* claude agents should share the same memory, which is stored at `memory/` in the work dir and symlinked to the Claude project memory location (`~/.claude/projects/<project>/memory/`, which then causes the memories to show up as changes to git). This is done during provisioning: since we know the work_dir, we can compute the Claude project directory name (absolute path with / and . replaced by -) and proactively create the directory and symlink without needing to poll
- Any claude agents should use the "project" memory scope (again, to keep memories version controlled)
- As part of getting itself set up, the ClaudeZygoteAgent will need to ensure that we've installed the "llm" tool, as well as our plugins for it (ie, "llm-anthropic" and "llm-live-chat"). In other words, we need to call these commands:
        uv tool install llm
        llm install llm-anthropic
        llm install llm-live-chat

All of the above is basically stuff that should either be done directly by the ClaudeZygoteAgent, or that it should configure such that everything works out (eg, shipping over bash scripts for the "chat" command, etc.)
