# Forever Claude Plugin

A much simpler persistent Claude agent, replacing the complex `mngr_claude_mind` + `mngr_llm` + `mngr_mind` stack.

## Overview

* The current `mngr_claude_mind` system is overly complex: it depends on an LLM toolchain (llm CLI + plugins), a JSON event stream pipeline, a conversation watcher, a web server, role-based agent separation (thinking/working/verifying/talking), and ~34 skills. Most of this complexity is unnecessary for a persistent agent that simply needs to stay alive, communicate via Telegram, and do work.
* This spec defines a `mngr_forever_claude` plugin (new lib in the monorepo) that registers a `ForeverClaudeAgent` subclass of `ClaudeAgent`, and a companion template repo (separate git repo, structured like `janktown`) that contains all the runtime scripts, skills, prompts, and configuration.
* The plugin is minimal: it registers the agent type, injects two extra tmux windows (bootstrap + telegram bot), validates required env vars, and configures trust/permissions. Everything else -- stop hook, wait script, skills, services -- lives in the template repo.
* Communication with the user is via Telegram (bot token + username filtering). No LLM conversations, no web server, no event batches.
* The agent is self-modifying: it can edit its own PURPOSE.md, skills, CLAUDE.md, and services.toml. It starts with a blank purpose and must ask the user what to do.

## Expected Behavior

### Agent creation

* User runs `mngr create my-agent forever-claude --project ~/project/my-forever-claude-template`
* Plugin validates that `TELEGRAM_BOT_TOKEN` and `TELEGRAM_USER_NAME` env vars are set (either in the environment, passed via `--pass-env`, or in a `.env` file). Fails with a clear error if missing.
* Plugin injects two extra tmux windows via `override_command_options`:
  - `bootstrap`: runs `uv run bootstrap` (from `libs/bootstrap/`) which reads `services.toml`, reconciles tmux windows, and watches for changes
  - `telegram`: runs `uv run telegram-bot` (from `libs/telegram_bot/`) which long-polls Telegram's `getUpdates` API and delivers messages via `mngr message`
* Agent starts in the template repo's work_dir with `bypassPermissionsModeAccepted=True`, `trust_working_directory=True`, `model="opus[1m]"`, `is_fast=True`
* `sync_home_settings=True` and `sync_claude_credentials=True` so the user's auth flows through

### Agent runtime

* Claude reads its CLAUDE.md and PURPOSE.md on startup
* PURPOSE.md initially says: "Your current purpose is to figure out your purpose. Ask the user via telegram (using your send-telegram-message skill) what they would like you to do. Once you know, replace this file with your actual purpose and begin executing on it."
* Claude uses the `send-telegram-message` skill to message the user
* The telegram bot delivers user replies via `mngr message`, formatted as `"telegram message from @username: <text>"` (if extra data like photos/files, include path reference: `"telegram message from @username (details in .runtime/telegram/media/<id>): <text>"`)
* When Claude finishes processing and tries to stop, the Stop hook always exits 2 and prints a reminder to check PURPOSE.md and run the wait script
* The wait script (`scripts/wait.sh`) implements idle backoff using a counter file (`.runtime/wait_counter`):
  - Reads the counter value (default 0 if file missing)
  - Looks up sleep duration from a schedule list (e.g. `[1, 1, 5, 10, 30, 60]` minutes; last value repeats)
  - Sleeps for that duration
  - Increments counter and writes it back
* A `UserPromptSubmit` Claude hook deletes `.runtime/wait_counter`, resetting the backoff when a real message arrives
* A `Notification[idle_prompt]` hook also deletes the counter file
* Claude can modify services.toml to add/remove background services. The bootstrap script watches for changes and reconciles tmux windows (starts new services, stops removed ones).
* Claude commits its own changes locally (no remote pushing). `.runtime/` and `memory/` are gitignored.

### Telegram bot

* Long-running Python process in `libs/telegram_bot/`
* Long-polls `getUpdates` with `timeout=30` (native Telegram long polling)
* Filters messages by `TELEGRAM_USER_NAME` (case-insensitive match on `message.from.username`)
* Appends the raw Telegram update JSON (one object per line) to `.runtime/telegram/history.jsonl`
* For each new text message, calls `mngr message $MNGR_AGENT_NAME -m "telegram message from @username: <text>"`
* Tracks `offset` for `getUpdates` to avoid reprocessing

### Telegram send CLI

* CLI tool in `libs/telegram_bot/`: `uv run telegram-send "message text"`
* Calls Telegram Bot API `sendMessage` with the bot token
* Looks up chat_id from the username by scanning recent history in `.runtime/telegram/history.jsonl` (finds the most recent chat_id for the configured username)
* Appends the sent message to `.runtime/telegram/history.jsonl` as well (with a `"direction": "out"` wrapper or similar distinguishing field)

### Bootstrap service manager

* Long-running Python process in `libs/bootstrap/`
* Reads `services.toml` on startup, parses `[services.<name>]` entries (each has `command = "..."` and optional `restart = "on-failure"`)
* Creates tmux windows in the agent's session for each service (using `tmux new-window -t <session>`)
* Watches `services.toml` for changes (inotifywait on Linux, polling fallback)
* On change: re-reads config, diffs against currently running tmux windows, starts new services, kills removed services
* Does not manage the windows injected by the plugin (bootstrap itself, telegram) -- only manages windows it created

### `mngr message` concurrency safety

* Add file-based locking (`flock`) in `BaseAgent.send_message()` so only one message can be sent to an agent at a time
* Lock file: `$MNGR_AGENT_STATE_DIR/message.lock` (or similar)
* This affects all agent types (simple, correct, and the right fix since concurrent tmux send-keys has always been unsafe)

### Skills in template repo

* `send-telegram-message`: How to send a message to the user via `uv run telegram-send "text"`. Includes guidance on tone, formatting, and when to proactively reach out.
* `read-telegram-history`: How to read `.runtime/telegram/history.jsonl` to understand conversation context. Includes example jq/python commands to extract recent messages, filter by direction, etc.
* `edit-services`: How to modify `services.toml` to add, update, or remove background services. Explains the format, what happens on change (bootstrap reconciles), and restart policies.
* `dealing-with-the-unexpected`: Updated for the forever-claude context. Guidance on what to do when something unexpected happens, how to debug, when to ask the user for help.

### Claude hooks in template repo (`.claude/settings.local.json`)

* `UserPromptSubmit`: `rm -f .runtime/wait_counter` (reset idle backoff)
* `Notification[idle_prompt]`: `rm -f .runtime/wait_counter` (reset idle backoff)
* `Stop`: runs `scripts/stop_hook.sh` which always exits 2 and prints: "You are a persistent agent. Check PURPOSE.md to understand your current goal and purpose. Run scripts/wait.sh to wait for the next message rather than ending your conversational turn."

### CLAUDE.md content

* Identity: you are a persistent Claude agent that runs continuously
* Communication: use the `send-telegram-message` skill to talk to the user; incoming messages arrive via `mngr message` from the telegram bot
* Self-modification: you can edit your own PURPOSE.md, CLAUDE.md, skills (create new ones, modify existing), and services.toml. Commit changes to git.
* Memory: use Claude's built-in memory system; memory directory is at `memory/` (configured via `autoMemoryDirectory` in settings)
* Idle behavior: when you have nothing to do, run `scripts/wait.sh` which sleeps with increasing backoff. Your wait resets when a new message arrives. Never end your conversational turn without running the wait script.
* Services: you can define background services in `services.toml` -- the bootstrap manager will start/stop tmux windows accordingly
* Git: commit your changes locally. `.runtime/` and `memory/` are gitignored. Do not push to remote.

### Template repo structure

```
CLAUDE.md                          # Main agent instructions
PURPOSE.md                        # Current purpose (initially: "figure out your purpose")
SOUL.md                           # Personality/values
.claude/
  settings.json                   # Permissions, model config
  settings.local.json             # Hooks (stop, UserPromptSubmit, idle_prompt)
skills/
  send-telegram-message/SKILL.md
  read-telegram-history/SKILL.md
  edit-services/SKILL.md
  dealing-with-the-unexpected/SKILL.md
scripts/
  wait.sh                         # Idle backoff wait script
  stop_hook.sh                    # Stop hook (always exit 2)
services.toml                     # Service definitions (default: simple python web server)
libs/
  telegram_bot/                   # Python package: telegram bot + send CLI
    pyproject.toml
    src/telegram_bot/
      bot.py                      # Long-polling bot
      send.py                     # Send CLI
      __init__.py
  bootstrap/                      # Python package: service manager
    pyproject.toml
    src/bootstrap/
      manager.py                  # Service reconciliation
      __init__.py
  mngr/                           # Vendored mngr monorepo (git subtree)
memory/                           # Claude memory dir (gitignored)
.runtime/                         # Runtime state (gitignored)
  telegram/
    history.jsonl                 # Telegram message history
  wait_counter                    # Idle backoff counter
.gitignore                        # Ignores .runtime/, memory/
pyproject.toml                    # Root project config (workspace)
uv.lock
```

### ForeverClaudeAgent plugin structure (in mngr monorepo)

```
libs/mngr_forever_claude/
  pyproject.toml
  imbue/mngr_forever_claude/
    __init__.py
    plugin.py                     # ForeverClaudeAgent, ForeverClaudeConfig, hookimpls
```

## Changes

* **New lib `libs/mngr_forever_claude/`**: Plugin that registers the `forever-claude` agent type. Contains `ForeverClaudeAgent(ClaudeAgent)` with `ForeverClaudeConfig(ClaudeAgentConfig)`. Implements `register_agent_type`, `override_command_options` (injects bootstrap + telegram extra windows), and env var validation in `on_before_provisioning`. Config defaults: `trust_working_directory=True`, `model="opus[1m]"`, `is_fast=True`, `sync_home_settings=True`, `sync_claude_credentials=True`. `_build_per_agent_claude_json` sets `bypassPermissionsModeAccepted=True`.
* **Modify `libs/mngr/imbue/mngr/agents/base_agent.py`**: Add `flock`-based locking around `send_message()` using a lock file at `$MNGR_AGENT_STATE_DIR/message.lock`. This makes concurrent `mngr message` calls safe for all agent types.
* **New template repo** (separate git repo, not in the mngr monorepo): Contains CLAUDE.md, PURPOSE.md, SOUL.md, skills, scripts, libs (telegram_bot, bootstrap), services.toml, and vendors the mngr monorepo as a git subtree under `libs/mngr/`. Structured as a Python monorepo with uv workspace.
* **No changes to**: `mngr_claude_mind`, `mngr_llm`, `mngr_mind`, or any existing plugin. The forever-claude plugin is entirely additive.
