# Forever Claude Template v2

A self-contained, self-modifying template repo for persistent Claude agents. No plugin required -- all configuration via `.mngr/settings.toml`.

## Overview

* The `mngr_forever_claude` plugin was over-engineered. Everything it did (agent type defaults, extra windows, env var validation) can be expressed through `.mngr/settings.toml` using custom agent types and create templates. The plugin should be deleted.
* The template repo is a standalone git repo (like `janktown`) that contains all agent configuration, skills, scripts, services, and libraries. It vendors `mngr` as a git subtree under `libs/mngr/`.
* The main agent is NOT persistent by default. Persistence (stop hooks, idle backoff) is available as an opt-in pattern via the `create-event-processor` skill, which creates a sub-agent from a pre-configured `event-processor/` directory.
* The agent can launch sub-agents via `mngr create` for delegated work, monitor them with `mngr wait` (as a background bash task), and pull results. Sub-agents are labeled with `mind=$MIND_NAME`.
* Communication with the user is via Telegram (bot + send CLI in `libs/telegram_bot/`).
* The agent can update itself by pulling from an upstream repo defined in `parent.toml`.

## Expected Behavior

### Agent type and create configuration (`.mngr/settings.toml`)

* `[agent_types.claude]`: sets `model = "opus[1m]"`, `is_fast = true`, `trust_working_directory = true`, `cli_args = "--dangerously-skip-permissions"`. These apply to ALL claude agents created from this repo.
* `[agent_types.main]`: sets `parent_type = "claude"`. Empty for now -- inherits everything from the claude type.
* `[commands.create]`: sets `pass_env = ["MIND_NAME"]`, `connect = false`, `ensure_clean = false`. These defaults apply to all creates including sub-agent creates.

### Create templates

* `worker`: For sub-agents created by the `launch-task` skill. Sets `type = "claude"`, adds reviewer env vars (`REVIEWER_AUTOFIX_ENABLE=1`, `REVIEWER_CI_ENABLE=1`, `REVIEWER_VERIFY_CONVERSATION_ENABLE=0`, `REVIEWER_AUTOFIX_MINOR=0`), the `create_reviewer_settings.sh` extra window, `--pass-env MIND_NAME`, and an `--append-system-prompt` with instructions: "You were launched by a mind agent. Your work will be reviewed via transcript and git diff. Commit your changes when done."
* `local`: Sets `extra_window = ["bootstrap=uv run bootstrap"]` for the bootstrap service manager.
* `modal`: Sets `provider = "modal"`, `idle_timeout`, a `github_setup` extra window (for SSH keyscan and git remote URL), `build_arg`, and the bootstrap extra window.
* `docker`: Sets `provider = "docker"` and the bootstrap extra window.

### Agent creation flow

* User runs: `mngr create my-mind main -t local --host-env MIND_NAME=my-mind --project ~/project/forever-claude-template`
* `MIND_NAME` is set as a host env var at creation time. `--pass-env MIND_NAME` in the create defaults ensures remote sub-agents inherit it.
* The bootstrap extra window starts the service manager, which reads `services.toml` and reconciles tmux windows.
* The telegram bot extra window (started via service or create template) long-polls for messages and delivers them via `mngr message`.

### Skills

**`send-telegram-message`**: Send a message to the user via `uv run telegram-send "text"`. Guidance on tone, formatting, providing options for the user to reply to.

**`read-telegram-history`**: Read `.runtime/telegram/history.jsonl` for conversation context. Documents `uv run telegram-history --last N` and raw jq/python examples.

**`launch-task`**: Create a sub-agent for larger work. Workflow:
1. Write task description to a file
2. `mngr create <name> -t worker --label mind=$MIND_NAME --message-file <file>`
3. Start `mngr wait <name> DONE STOPPED WAITING &` as a background bash task so the main agent can do other work
4. When the wait completes, check the agent's state
5. **Monitoring section**: `mngr list --active --label mind=$MIND_NAME --format jsonl` to see all tasks. `mngr transcript <name>` to read conversation. `mngr capture <name>` for current terminal. When agent transitions to WAITING: check if the verifier ran (likely finished) or if earlier assistant messages contain questions that need answers. If the agent asked a question, answer it via `mngr message` or decide to cancel/restart.
6. Results are accessible via git (sub-agents work on worktree branches) -- no need for `mngr pull`
7. Optionally `mngr destroy <name>` when done

**`create-event-processor`**: Create a persistent sub-agent from the `event-processor/` directory. Steps:
1. Write `event-processor/PURPOSE.md` describing what events to process and what to do with them
2. `mngr create <name> --type claude --transfer none --message "..." --label mind=$MIND_NAME` pointing at the `event-processor/` directory
3. The event processor stays alive via its stop hook, sleeps with backoff when idle, and wakes up when messages arrive

**`update-self`**: Pull updates from the upstream repo. Reads `parent.toml` for `url` and `branch`. Adds an `upstream` git remote if it doesn't exist (`git remote add upstream <url>`), then runs `git pull upstream <branch>`. The upstream URL may differ from the push remote (e.g., pulling from public upstream, pushing to private fork).

**`edit-services`**: Modify `services.toml` to add/update/remove background services. Documents the `[services.<name>]` format with `command` and optional `restart = "on-failure"`. Bootstrap manager watches the file and reconciles tmux windows.

**`dealing-with-the-unexpected`**: Guidance for when things go wrong. Gather information (check tmux windows, services, telegram history, wait counter), diagnose common issues, fix or escalate to the user via telegram.

### `event-processor/` directory

Pre-configured directory in the template repo for creating persistent sub-agents:

* `scripts/stop_hook.sh`: Always exits 2. Prints: "You are a persistent agent. Check PURPOSE.md for your goal. Run scripts/wait.sh to wait for the next message."
* `scripts/wait.sh`: Idle backoff with schedule `[1, 1, 5, 10, 30, 60]` minutes (last value repeats). Reads/increments `.runtime/wait_counter`.
* `.claude/settings.local.json`: Hooks for `UserPromptSubmit` (delete wait counter), `Notification[idle_prompt]` (delete wait counter), `Stop` (run stop_hook.sh).
* `PURPOSE.md`: Blank -- filled by the creating agent before launching.
* `CLAUDE.md`: Minimal instructions for being an event processor: check PURPOSE.md, communicate via mngr message back to parent, run wait.sh when idle.

### `parent.toml`

At repo root, version controlled:

```toml
url = "https://github.com/imbue-ai/forever-claude-template.git"
branch = "main"
```

Ships pre-populated pointing at the upstream template. Users change it if they fork.

### CLAUDE.md

* Identity: you are a persistent Claude agent that runs continuously
* Communication: telegram skills for user interaction; incoming messages arrive via `mngr message`
* Self-modification: edit PURPOSE.md, CLAUDE.md, skills, services.toml, scripts. Commit changes to git.
* Memory: built-in memory at `memory/` (autoMemoryDirectory). Gitignored.
* Work delegation: use `launch-task` skill for larger work, `create-event-processor` for background watchers. No strong default -- agent's judgment based on PURPOSE.md, user can set preference.
* Updates: use `update-self` skill to pull from upstream
* Services: use `edit-services` to manage background tmux windows
* Git: commit locally, do not push to remote. `.runtime/` and `memory/` are gitignored.

### Template repo structure

```
CLAUDE.md
PURPOSE.md
SOUL.md
parent.toml
.mngr/
  settings.toml                    # agent_types, create_templates, command defaults
.claude/
  settings.json                    # permissions, autoMemoryDirectory
  settings.local.json              # hooks (if any for main agent)
skills/
  send-telegram-message/SKILL.md
  read-telegram-history/SKILL.md
  launch-task/SKILL.md
  create-event-processor/SKILL.md
  update-self/SKILL.md
  edit-services/SKILL.md
  dealing-with-the-unexpected/SKILL.md
scripts/
  create_reviewer_settings.sh
event-processor/
  CLAUDE.md
  PURPOSE.md
  scripts/
    stop_hook.sh
    wait.sh
  .claude/
    settings.local.json
  .runtime/                        # gitignored
services.toml
libs/
  telegram_bot/                    # bot, send CLI, history viewer
    pyproject.toml
    src/telegram_bot/
  bootstrap/                       # service manager
    pyproject.toml
    src/bootstrap/
  mngr/                            # vendored mngr monorepo (git subtree)
memory/                            # gitignored
.runtime/                          # gitignored
.gitignore
pyproject.toml                     # root workspace config
uv.lock
```

### Libs

* `libs/telegram_bot/`: Long-polling bot (`telegram-bot` CLI), send CLI (`telegram-send`), history viewer (`telegram-history`). Uses stdlib `urllib` only. Appends raw update JSON to `.runtime/telegram/history.jsonl`. Filters by `TELEGRAM_USER_NAME`. Delivers via `mngr message $MNGR_AGENT_NAME`.
* `libs/bootstrap/`: Service manager. Reads `services.toml`, reconciles `svc-<name>` tmux windows. Polls for file changes. Starts new services, stops removed ones.
* `libs/mngr/`: Vendored mngr monorepo as git subtree.

## Changes

* **Delete `libs/mngr_forever_claude/`** from the mngr monorepo. Remove from `uv.lock`. The plugin is no longer needed.
* **Keep the flock-based message locking** in `libs/mngr/imbue/mngr/agents/base_agent.py`. This is a good general fix independent of the plugin.
* **Update the forever-claude-template repo** (separate git repo at `~/project/forever-claude-template`):
  - Add `.mngr/settings.toml` with agent types (`claude`, `main`), create templates (`worker`, `local`, `modal`, `docker`), and command defaults
  - Add `parent.toml` pointing at the upstream repo
  - Add skills: `launch-task`, `create-event-processor`, `update-self` (new). Update existing skills.
  - Add `event-processor/` directory with stop hook, wait script, hooks, blank PURPOSE.md, minimal CLAUDE.md
  - Add `scripts/create_reviewer_settings.sh`
  - Update CLAUDE.md with delegation, self-update, and event processor guidance
  - Remove stop hook / wait script / persistent hooks from the main agent (move to event-processor/)
* **No changes to** `mngr_claude`, `mngr_claude_mind`, `mngr_llm`, `mngr_mind`, or any other existing plugin.
