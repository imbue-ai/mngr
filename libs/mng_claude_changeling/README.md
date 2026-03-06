# Design for "Changelings"

This plugin implements the core functionality for claude-based "changelings": LLM-based agents that can have multiple conversation threads, react to events, and store persistent memories.

The core idea is to have a "primary" agent that serves as the "inner monologue" of the changeling, and that reacts to events (like new messages in conversation threads, scheduled events, sub-agent state changes, etc.)
Rather than having a direct conversation with the agent, the agent has a prompt that tells it what to do in response to various events, and it simply processes those until it decides that everything is complete and it can go to sleep (until it is awoken to process the next event)

In practice, "changelings" are just special mng agents that inherit from the ClaudeChangelingAgent

They can be thought of as a sort of "higher level" agent that is made by assembling a few different LLM-based programs

Fundamentally, changelings are mng agents where:
- The primary agent (from mng's perspective) is a claude code instance *that reacts to "events"* and forms the sort of "inner dialog" of the agent
- Users do *not* chat directly with this main "inner monolouge" agent--instead, they have conversational threads via the "llm live-chat" command-line tool, and those conversations are *exposed* to the primary agent by sending it events to react to.
- The core "inner monologue" agent can be thought of as reacting to events. It is sent messages (by a watcher, via "mng message") whenever new events appear in the event streams. For example:
    - `events/messages/events.jsonl`: new conversation messages synced from the llm database (we'll need to filter out any where the source was the primary agent, since it obviously should not be notified for its own messages)
    - `events/scheduled/events.jsonl`: one of the time based triggers happens ("mng schedule" can be used, via a skill, to schedule custom events at certain times, which in turn append the json data for that event)
    - `events/mng_agents/events.jsonl`: a sub agent (launched by this primary agent) transitions to "waiting" (happens via our own hooks--goes through a modal hook like snapshot_and_save if remote, otherwise can just create directly)
    - `events/stop/events.jsonl`: the primary agent tries to stop (for the first time--before thought complete, roughly). This allows it to do a last check of whether there is anything else worth responding to before going to sleep (and considering when it ought to wake)
    - `events/monitor/events.jsonl`: (future) a local monitor agent appends a message/reminder to its file
- The primary agent is generally instructed to do everything via "mng" (because then all sub agents and work is visible / totally transparent to you)

## Event log structure

All event data uses a consistent append-only JSONL format stored under `<agent-data-dir>/events/<source>/events.jsonl`. Every event line has a standard envelope:

    {"timestamp": "...", "type": "...", "event_id": "...", "source": "<source>", ...additional fields}

Event sources:
- `events/conversations/events.jsonl` - conversation lifecycle events (created, model changed). Each event includes `conversation_id` and `model`.
- `events/messages/events.jsonl` - all conversation messages across all conversations. Each event includes `conversation_id`, `role`, and `content`.
- `events/scheduled/events.jsonl`: Each event corresponds to a scheduled trigger that the primary agent should react to. The event data includes the name of the trigger and any relevant metadata.
- `events/mng_agents/events.jsonl`: all relevant agent state transitions (eg, when they become blocked, crash, finish, etc). Each event includes the agent_id, the new state, and any relevant metadata about the transition (eg, error message if it crashed)
- `events/stop/events.jsonl`: for detecting when this agent tried to stop the first time
- `events/monitor/events.jsonl`: (future) for injecting metacognitive thoughts or reminders from a local monitor agent
- `events/delivery_failures/events.jsonl`: event delivery failure notifications (written by event_watcher.py when it cannot deliver events to the primary agent)
- `logs/claude_transcript/events.jsonl` - inner monologue transcript (raw Claude JSONL, written by stream_transcript.sh).

Every event is self-describing: you never need to know the filename to understand the event. The file organization is a performance/convenience choice, not a correctness one.

## Implementation details

- The "conversation threads" or "chat threads" are simply conversation ids that are tracked by the "llm" tool (a 3rd party CLI tool that is really nice and simple--we've made a plugin for it, llm-live-chat, that enables the below "llm live-chat" and "llm inject" commands)
- Users create new (and resume existing) conversations by calling a little "chat" command. It's just a little bash script that creates event json entries and also makes calls to "llm" so that users and agents don't need to remember the exact invocations. "chat --new" for a new chat and "chat --resume <conversation_id>" to resume. "chat" with no arguments lists all current conversation ids
- Agents create new conversations by using their "new chat" skill, which calls "chat --new --as-agent" and passing in the message as well
- Whenever the user (or the agent) creates a new conversation, the "chat" wrapper appends a `conversation_created` event to `events/conversations/events.jsonl` (with the standard envelope plus `conversation_id` and `model`). The conversation is started by calling "llm live-chat" (for user messages) or "llm inject" (for agent messages)
- The ClaudeChangelingAgent runs a conversation watcher script in a tmux window that watches the llm database and, whenever it changes, syncs new messages to `events/messages/events.jsonl` (with the standard envelope plus `conversation_id`, `role`, `content`)
- Thus the URL to view an existing chat conversation is simply done via a special ttyd server that runs the correct llm invocation: "llm live-chat --show-history -c --cid <conversation_id> -m <chat-model>" where chat-model comes from the most recent event in `events/conversations/events.jsonl` with that conversation_id
- To list all conversations for this agent, we read `events/conversations/events.jsonl` (append-only, last value per conversation_id wins)
- When invoking "llm live-chat", we pass in two tools: one for gathering context (recent messages from other conversations, inner monologue, recent events) and another for extra context (mng agent list, deeper history)
- A simple event watcher observes the event streams (`events/messages/events.jsonl`, `events/scheduled/events.jsonl`, `events/mng_agents/events.jsonl`, `events/stop/events.jsonl`) for changes, and when modified, sends the next unhandled event(s) to the primary agent (via "mng message")
- Changeling agents are assumed to run from a specially structured git repo that contains various skills, configuration, prompt files, and the code for any tools they have constructed for themselves. The layout is:
    - `GLOBAL.md` - shared instructions for all agents (symlinked as `CLAUDE.md` so Claude Code discovers it)
    - `talking/` - the talking agent (generates replies to user messages, runs via llm, not Claude Code)
    - `thinking/` - primary/inner monologue agent (that reacts to events, can have skills and tools)
    - `working/` - the working agent (does the actual work, can have skills and tools)
    - `verifying/` - the verifying agent (scheduled in reaction to "finished" events from sub-agents, checks work, can have skills and tools)
    - `(user-defined roles)/` - any additional agent roles the user wants to define (e.g. "planning/", "researching/", etc)
- Each changeling agent has an "active role" (configured via `active_role` in `ClaudeChangelingConfig`, default: `"thinking"`). The repo root `.claude/` is a directory symlink to `<active_role>/.claude/`, so Claude Code naturally discovers that role's settings, skills, etc.
- Each role (except `talking/`) has its own directory structure:
    - `<role>/PROMPT.md` - prompt for the agent (symlinked as `CLAUDE.local.md` when active)
    - `<role>/.claude/settings.json` - Claude Code settings for the role
    - `<role>/.claude/skills/` - skills for the role
    - `<role>/.claude/settings.local.json` - mng-managed hooks (gitignored, written during provisioning)
    - `<role>/memory/` - per-role memory directory (synced into Claude project memory via hooks)
- The `GLOBAL.md` serves as the core system prompt that is *shared* among all agents (the primary agent, any claude subagent it makes, and even any other agents created via mng with this repo as the target). It is symlinked to `CLAUDE.md` at the project root so Claude Code picks it up.
- When a role is active, provisioning creates: `.claude` -> `<active_role>/.claude`, `CLAUDE.md` -> `GLOBAL.md`, and `CLAUDE.local.md` -> `<active_role>/PROMPT.md`. All of this is handled by the ClaudeChangelingAgent during provisioning.
- Other agent roles can be defined by creating corresponding directories with their own `PROMPT.md` and `.claude/` subdirectories (except `talking/`, which can only have `PROMPT.md`). An appropriate agent type must also be created for them in `.mng/settings.toml` right now.
- The prompts for the primary agent (both before shutdown and upon message receipt) should encourage it to keep track of messages that it received (via its own task list)
- Each role has its own memory stored at `<role>/memory/` in the work dir and synced to the Claude project memory location (`~/.claude/projects/<project>/memory/`) via PreToolUse/PostToolUse hooks. This keeps memories version-controlled in git.
- Any claude agents should use the "project" memory scope (to keep memories version controlled)
- As part of getting itself set up, the ClaudeChangelingAgent will need to ensure that we've installed the "llm" tool, as well as our plugins for it (ie, "llm-anthropic" and "llm-live-chat"). In other words, we need to call these commands:
        uv tool install llm
        llm install llm-anthropic
        llm install llm-live-chat

All of the above is basically stuff that should either be done directly by the ClaudeChangelingAgent, or that it should configure such that everything works out (eg, shipping over bash scripts for the "chat" command, etc.)
