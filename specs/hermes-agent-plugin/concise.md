# Hermes Agent Plugin for mngr

## Overview

* Add a new `hermes` agent type to mngr, allowing `mngr create --type hermes` to spawn interactive Hermes Agent sessions in tmux -- the same workflow as the `claude` agent type
* Hermes is an external AI agent framework (Nous Research) with its own TUI (prompt_toolkit), config system (`~/.hermes/config.yaml`, `~/.hermes/.env`), and multi-instance isolation via `HERMES_HOME` env var
* The plugin lives in the mngr monorepo as `libs/mngr_hermes`, following the same structure as `libs/mngr_opencode`
* Each mngr-managed hermes agent gets an isolated `HERMES_HOME` inside its agent state directory (`$MNGR_AGENT_STATE_DIR/hermes_home/`), seeded from the user's existing `~/.hermes` during provisioning
* The plugin uses a thin custom agent class extending `BaseAgent` -- overrides `modify_env_vars` (to inject `HERMES_HOME`) and `provision` (to seed the hermes home directory) while reusing BaseAgent for all other behavior
* Assumes `hermes` is pre-installed on the host (no preflight validation)

## Expected Behavior

* `mngr create --type hermes` spawns a `hermes chat` session inside a tmux pane, identical to how `--type claude` spawns claude code
* The agent's hermes instance uses `$MNGR_AGENT_STATE_DIR/hermes_home/` as its `HERMES_HOME`, fully isolated from the user's default `~/.hermes` and from other hermes agents
* During provisioning, the following are copied from `~/.hermes` to the per-agent `HERMES_HOME`:
  - `config.yaml` -- model, display, terminal backend settings
  - `.env` -- API keys and secrets
  - `memories/` -- agent memory files (MEMORY.md, USER.md)
  - `skills/` -- user-created and synced skills
  - `home/` -- per-profile subprocess isolation (git, ssh, npm configs)
  - `auth.json` -- OAuth tokens, pooled API credentials
  - `SOUL.md` -- agent personality file
* The following are intentionally NOT seeded (runtime state that should start fresh): `state.db`, `sessions/`, `logs/`, `plans/`, `cron/`, `skins/`, `.hermes_history`
* If `~/.hermes` does not exist on the source machine, seeding is silently skipped -- hermes handles its own first-time setup
* The agent's hermes config is mutable at runtime (hermes can modify its own config.yaml), but changes are lost on agent destroy since the next create re-seeds from `~/.hermes`
* For remote hosts, the local machine's `~/.hermes` contents are transferred to the remote host during provisioning
* `mngr attach`, `mngr send`, `mngr list`, and other standard mngr commands work with hermes agents via the inherited BaseAgent tmux integration
* Users can pass hermes-specific CLI flags via `mngr create --type hermes -- -m anthropic/claude-sonnet-4 -t code,web` (agent_args forwarded to `hermes chat`)

## Changes

* Create new package `libs/mngr_hermes/` with the standard mngr plugin structure:
  - `pyproject.toml` -- package metadata, `imbue-mngr` dependency, `[project.entry-points.mngr]` registering `hermes = "imbue.mngr_hermes.plugin"`
  - `imbue/mngr_hermes/__init__.py` -- blank (with `hookimpl` marker if needed)
  - `imbue/mngr_hermes/plugin.py` -- `HermesAgentConfig` (extends `AgentTypeConfig`, sets default command to `hermes chat`), `HermesAgent` (extends `BaseAgent`, overrides `modify_env_vars` and `provision`), and the `register_agent_type` hookimpl
* `HermesAgent.modify_env_vars`: injects `HERMES_HOME` pointing to `$MNGR_AGENT_STATE_DIR/hermes_home/`
* `HermesAgent.provision`: creates the `hermes_home/` directory inside the agent state dir, then copies the enumerated config files and directories from the local `~/.hermes` (or the source host's `~/.hermes`) using `host.copy_directory` with appropriate exclusions; silently skips if source `~/.hermes` does not exist; handles both individual files (`config.yaml`, `.env`, `auth.json`, `SOUL.md`) and directories (`memories/`, `skills/`, `home/`)
* Register the new package in the workspace `pyproject.toml` (members list and dependency sources) -- the package is discovered automatically by pluggy via its `[project.entry-points.mngr]` entry point when installed, not added as a dependency of the core `imbue-mngr` package (same pattern as `imbue-mngr-opencode` and `imbue-mngr-claude`)
