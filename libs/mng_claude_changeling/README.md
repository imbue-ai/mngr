# mng_claude_changeling

This plugin implements the core functionality for Claude-based "changelings": composite agents built from multiple mng agents that, together, form a single higher-level agent from the user's perspective.

## What is a changeling?

A changeling is a *collection of persistent mng agents* that serve a web interface and support chat input/output. Each mng agent within the changeling fulfills a specific **role** (e.g., thinking, working, verifying), and they coordinate through shared event streams in a common git repo.

From mng's perspective, each role is a separate mng agent with its own agent type. Multiple instances of a single role can run simultaneously (e.g., multiple workers). The plugin is responsible for transforming a simple directory of text files (prompts, skills, configuration) into a valid set of mng agent configurations.

### Terminology

To avoid confusion, this project uses precise terminology for three distinct concepts:

- **Role agent**: A standard mng agent (a process in a tmux session, visible in `mng list`, with its own lifecycle state). Each role agent is created via `mng create` and fulfills a specific role within the changeling.
- **Changeling**: The collection of role agents that together form a single higher-level agent from the user's perspective. Not itself an mng agent -- it's the emergent whole.
- **Supporting service**: A background process running in a tmux window alongside the primary role agent (e.g., watchers, web server, ttyd). These are *not* mng agents -- they don't appear in `mng list`, can't be messaged, and have no lifecycle state. They are infrastructure that the plugin provisions automatically.

## Architecture

### Roles as agent types

Each role directory in the changeling repo corresponds to an mng agent type:

- `thinking/` - the primary role agent; the "inner monologue" that reacts to events and coordinates the changeling
- `talking/` - generates replies to user messages (runs via `llm live-chat`, not Claude Code)
- `working/` - executes actual work tasks, can have skills and tools
- `verifying/` - checks work quality, triggered by "finished" events from other role agents
- `(user-defined)/` - any additional roles (e.g., "planning/", "researching/")

Each role (except `talking/`) has its own directory structure:

- `<role>/PROMPT.md` - role-specific prompt (symlinked as `CLAUDE.local.md` within the role directory)
- `<role>/.claude/settings.json` - Claude Code settings for the role
- `<role>/.claude/skills/` - skills available to the role
- `<role>/.claude/settings.local.json` - mng-managed hooks (gitignored, written during provisioning)
- `<role>/memory/` - per-role memory (synced into Claude project memory via hooks)

Claude Code runs from within the role directory (via `cd $ROLE` in `assemble_command`), so `.claude/` is discovered naturally. `GLOBAL.md` at the repo root is symlinked as `CLAUDE.md` and discovered by Claude Code walking up the directory tree.

### Role agent lifecycle

The **thinking** role agent is the first role agent created when a changeling is deployed. It is the coordinator: it decides when to create other role agents based on the events it receives.

When the thinking agent determines that work needs to be done, it creates other role agents by calling `mng create`. Each created role agent is a fully independent mng agent -- it appears in `mng list`, has its own lifecycle state, and runs in its own tmux session. Multiple instances of the same role can exist simultaneously (e.g., several workers tackling different tasks in parallel).

The thinking agent learns about role agent state changes via the `events/mng/agents/events.jsonl` event stream. When a role agent transitions to "waiting", "done", or "crashed", an event is written and delivered to the thinking agent, which can then decide how to proceed (e.g., verify work, retry, or report to the user).

All role agents share the same git repo as their working directory, and they all see the same `GLOBAL.md` instructions. Each role agent's specific behavior is determined by its role directory (prompt, skills, settings).

### The primary role agent (thinking role)

The "thinking" role is the default primary role agent. It does not chat directly with users. Instead, it reacts to events delivered from shared event streams:

- `events/messages/events.jsonl` - new conversation messages (synced from the `llm` database)
- `events/scheduled/events.jsonl` - time-based triggers
- `events/mng/agents/events.jsonl` - role agent state transitions (waiting, crashed, done, etc.)
- `events/stop/events.jsonl` - shutdown detection (last chance to check for pending work)
- `events/monitor/events.jsonl` - (future) metacognitive reminders from a monitor agent

### Conversation system

Conversations are stored in the `llm` tool's SQLite database, which serves as the authoritative source of all chat data. The system provides multiple interfaces for interacting with that database:

- **Users** chat via `llm live-chat` through a ttyd web terminal or the `chat` bash script
- **Role agents** post messages via `llm inject` (through skills like "send-message-to-user")
- **The conversation watcher** (a supporting service) syncs new messages from the database to `events/messages/events.jsonl`
- **The event watcher** (a supporting service) delivers those events to the primary role agent via `mng message`

This means: user sends message -> `llm` database -> conversation watcher syncs to events -> event watcher delivers to primary role agent -> primary role agent uses skill to call `llm inject` -> response appears in `llm` database -> user sees it in `llm live-chat`.

### Supporting services (tmux windows)

The primary role agent is augmented with several supporting services running in additional tmux windows. These are *not* mng agents -- they are background processes that the plugin provisions automatically:

- **Conversation watcher** - polls the `llm` SQLite database and syncs new messages to `events/messages/events.jsonl`
- **Event watcher** - monitors event streams and delivers new events to the primary role agent via `mng message`
- **Transcript watcher** - converts raw Claude transcript to a common agent-agnostic format
- **Web server** - serves the main web interface with conversation selector and agent list
- **Chat ttyd** - provides web-terminal access to conversations via `llm live-chat`
- **Agent ttyd** - provides web-terminal access to the primary role agent's tmux session

## Settings

Per-deployment settings are read from `changelings.toml` in the agent work directory (`$MNG_AGENT_WORK_DIR/changelings.toml`). This file is optional -- all settings have built-in defaults. See `ClaudeChangelingSettings` in `data_types.py` for the full schema.

## Event log structure

All event data uses a consistent append-only JSONL format stored under `<agent-data-dir>/events/<source>/events.jsonl`. Every event line has a standard envelope:

    {"timestamp": "...", "type": "...", "event_id": "...", "source": "<source>", ...additional fields}

Event sources:
- `events/messages/events.jsonl` - all conversation messages across all conversations
- `events/scheduled/events.jsonl` - scheduled triggers
- `events/mng/agents/events.jsonl` - role agent state transitions
- `events/stop/events.jsonl` - shutdown detection
- `events/monitor/events.jsonl` - (future) metacognitive reminders
- `events/delivery_failures/events.jsonl` - event delivery failure notifications
- `events/common_transcript/events.jsonl` - agent-agnostic transcript format
- `logs/claude_transcript/events.jsonl` - raw Claude transcript

Every event is self-describing: you never need to know the filename to understand the event. The file organization is a performance/convenience choice, not a correctness one.

Conversation metadata (tags, created_at) is stored in the `changeling_conversations` table in the llm sqlite database (`$LLM_USER_PATH/logs.db`). The model for each conversation lives in the llm tool's native `conversations` table.

## Provisioning

The `ClaudeChangelingAgent.provision()` method transforms the changeling repo into a running role agent:

1. Loads settings from `changelings.toml`
2. Validates role constraints (e.g., `talking/` cannot have `.claude/` or skills)
3. Installs the `llm` toolchain (`llm`, `llm-anthropic`, `llm-live-chat`)
4. Provisions default content (GLOBAL.md, role prompts, role configs) for any missing files
5. Creates symlinks (`CLAUDE.md` -> `GLOBAL.md`, `<role>/CLAUDE.local.md` -> `<role>/PROMPT.md`)
6. Configures hooks (readiness detection + memory sync) in `<role>/.claude/settings.local.json`
7. Deploys supporting service scripts and chat utilities to the host
8. Creates the event log directory structure
9. Sets up per-role memory directories with sync hooks

## Dependencies

This plugin depends on:
- `mng` - the core agent management framework
- `mng-ttyd` - ttyd integration for web terminal access
- `watchdog` - filesystem event monitoring for supporting services
