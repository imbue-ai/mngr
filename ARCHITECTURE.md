# Architecture Overview

This document provides a comprehensive overview of the `mng` monorepo -- its structure, core abstractions, design patterns, and how everything fits together.

## What is mng?

`mng` is a CLI tool for creating and managing AI coding agents (Claude Code, Codex, OpenCode, etc.) that can run locally or remotely. It is built on standard open-source tools (SSH, git, tmux, Docker) and is extensible via a plugin system.

## Monorepo Structure

```
mng/
  libs/                          # Libraries
    mng/                         # Core library (the main product)
    imbue_common/                # Shared primitives, models, and utilities
    concurrency_group/           # Structured thread/process management
    mng_pair/                    # Plugin: continuous file sync (mng pair)
    mng_opencode/                # Plugin: OpenCode agent type
    mng_schedule/                # Plugin: cron-scheduled agent runs
    mng_kanpan/                  # Plugin: TUI agent tracker dashboard
    mng_tutor/                   # Plugin: interactive tutorials
    flexmux/                     # FlexLayout-based tab manager with Flask backend
  apps/                          # Applications
    changelings/                 # Experimental autonomous agent scheduler
    claude_web_view/             # Web viewer for Claude Code transcripts (FastAPI + React)
    sculptor_web/                # Web interface component
  scripts/                       # Build and utility scripts
  style_guide.md                 # Coding standards
  CLAUDE.md                      # Instructions for Claude Code agents working on this repo
  pyproject.toml                 # Monorepo config (uv workspace, pytest, ruff, import-linter)
```

All libraries and apps are part of a single **uv workspace** (`[tool.uv.workspace] members = ["libs/*", "apps/*"]`). The shared Python namespace is `imbue.*` (e.g., `imbue.mng`, `imbue.imbue_common`, `imbue.changelings`).

## Core Concepts

The three fundamental abstractions are **agents**, **hosts**, and **providers**:

- **Agent**: A process running in window 0 of a `mng-<agent_name>` tmux session. Each agent has a name, a unique ID, an agent type (claude, codex, etc.), a working directory, and a lifecycle state (stopped, running, waiting, replaced, done).

- **Host**: An isolated sandbox where agents run. A host can be the local machine, a Docker container, a Modal sandbox, or any SSH-accessible machine. Multiple agents can share a single host. Hosts have their own lifecycle (building, starting, running, stopping, stopped, paused, crashed, failed, destroyed) and automatically pause when all their agents are idle.

- **Provider**: A configured endpoint that creates and manages hosts. Provider **backends** (local, docker, modal, ssh) are stateless factories; provider **instances** are configured endpoints created from backends (e.g., "my-aws-prod" using the "aws" backend with specific region/profile settings).

### State Model

`mng` stores almost no persistent state. Instead, it reconstructs everything from:

1. **Provider queries** -- inspecting Docker labels, Modal tags, local state files, etc.
2. **Host queries** -- checking SSH connectivity, reading agent state from the filesystem
3. **Configuration files** -- settings, enabled plugins, etc.

This means no database, no state corruption risk, and multiple `mng` instances can manage the same agents simultaneously.

### Conventions

`mng` relies on naming conventions to identify managed resources:

- Tmux sessions are named `mng-<agent_name>` (prefix customizable via `MNG_PREFIX`)
- Agent state lives at `$MNG_HOST_DIR/agents/$MNG_AGENT_ID/`
- Host state lives at `$MNG_HOST_DIR/` (default: `~/.mng`)
- IDs are base16-encoded UUID4s with type prefixes (e.g., `agent-abc123...`, `host-def456...`)
- Names are human-readable strings containing only letters, numbers, and hyphens

## libs/mng -- Core Library Architecture

The core `mng` library follows a strict **layered architecture**, enforced by `import-linter`:

```
main           # Entry point, plugin manager initialization
  |
cli            # Click command implementations (create, list, destroy, etc.)
  |
api            # High-level operations (create_agent, list_agents, push, pull, etc.)
  |
agents         # Agent type implementations (ClaudeAgent, CodexAgent, etc.)
  |
providers      # Provider backends (local, docker, modal, ssh)
  |
hosts          # Host abstraction (online host, offline host)
  |
interfaces     # Abstract base classes (AgentInterface, HostInterface, ProviderInstanceInterface)
  |
config         # Configuration loading (TOML files, env vars, CLI args, plugin registry)
  |
utils          # Shared utilities (git, rsync, SSH, file I/O, name generation, etc.)
  |
errors         # Error hierarchy
  |
primitives     # Domain types (AgentId, HostName, HostState, CommandString, etc.)
```

Each layer may only import from layers below it. This is checked in CI.

### Key Interfaces

**`AgentInterface`** (`interfaces/agent.py`) -- the abstract agent contract:
- Identity: `id`, `name`, `agent_type`, `work_dir`, `create_time`, `host_id`
- Commands: `assemble_command()`, `get_command()`, `get_expected_process_name()`
- State: `is_running()`, `get_lifecycle_state()`, `get_permissions()`, `get_labels()`
- Interaction: `send_message()`, `capture_pane_content()`, `wait_for_ready_signal()`
- Activity: `get_reported_activity_time()`, `record_activity()`
- Provisioning: `on_before_provisioning()`, `get_provision_file_transfers()`, `provision()`, `on_after_provisioning()`
- Destruction: `on_destroy()`

**`HostInterface`** (`interfaces/host.py`) -- the abstract host contract:
- Identity: `id`, `is_local`, `host_dir`, `get_name()`
- State: `get_state()`, `get_certified_data()`, `get_activity_config()`
- Extended by `OnlineHostInterface` for hosts that are currently accessible, adding: `execute_command()`, `read_file()`, `write_file()`, `get_agents()`, `create_agent_work_dir()`, `provision_agent()`, `start_agents()`, `stop_agents()`, etc.

**`ProviderInstanceInterface`** (`interfaces/provider_instance.py`) -- manages host lifecycle:
- Capabilities: `supports_snapshots`, `supports_shutdown_hosts`, `supports_volumes`
- Lifecycle: `create_host()`, `stop_host()`, `start_host()`, `destroy_host()`
- Discovery: `get_host()`, `list_hosts()`, `load_agent_refs()`
- Snapshots: `create_snapshot()`, `list_snapshots()`, `delete_snapshot()`
- Volumes: `list_volumes()`, `delete_volume()`

**`ProviderBackendInterface`** (`interfaces/provider_backend.py`) -- stateless factory:
- `get_name()`, `get_description()`, `get_config_class()`, `build_provider_instance()`

### Providers

Four provider backends ship with `mng`:

| Backend | Description | Isolation | Snapshots | Use Case |
|---------|------------|-----------|-----------|----------|
| **local** | Runs on your machine directly | None | No | Quick local dev |
| **docker** | Docker containers | Container-level | No | Local isolation |
| **modal** | Modal.com Sandboxes | VM-level | Yes | Cloud compute, auto-shutdown |
| **ssh** | Any SSH-accessible host | Depends on host | No | Pre-existing infrastructure |

### Agent Types

Default agent implementations live in `agents/default_plugins/`:

- **ClaudeAgent** -- Claude Code with configurable model, permissions, and provisioning
- **CodexAgent** -- OpenAI Codex CLI integration
- **SkillAgent** -- Runs a specific skill/script
- **CodeGuardianAgent** -- Code review agent
- **FixmeFairyAgent** -- Automated FIXME resolver

Additional agent types can be registered via plugins.

### Configuration

Configuration is loaded hierarchically (later sources override earlier):

1. Built-in defaults
2. Global config (`~/.mng/settings.toml`)
3. Local/project config (`.mng/settings.toml`)
4. Environment variables
5. CLI arguments

The `MngContext` dataclass holds the resolved configuration for a given invocation, including the mng data directory, user ID, enabled plugins, and provider instances.

### CLI Commands

The CLI is built with [Click](https://click.palletsprojects.com/) and organized into:

**Primary** (agent management): `create` (default), `destroy`, `connect`, `list`, `stop`, `start`, `exec`, `rename`

**Data transfer**: `pull`, `push`, `pair`, `message`

**Setup**: `provision`

**Secondary**: `cleanup`, `logs`, `gc`, `snapshot`, `limit`, `clone`, `migrate`, `ask`, `config`, `plugin`

## Plugin System

`mng` uses [pluggy](https://pluggy.readthedocs.io/) for its plugin system. Plugins are Python packages that declare an entry point under the `mng` group:

```toml
# In a plugin's pyproject.toml
[project.entry-points.mng]
my_plugin = "my_package.plugin"
```

### Hook Categories

**Registration hooks** (called once at startup):
- `register_agent_type` -- add new agent types
- `register_provider_backend` -- add new provider backends
- `register_cli_commands` -- add new CLI commands
- `register_cli_options` -- add options to existing commands

**Program lifecycle hooks** (called during execution):
- `on_load_config`, `on_startup`, `on_before_command`, `on_after_command`, `on_error`, `on_shutdown`
- `override_command_options` -- transform parsed args before execution

**Host lifecycle hooks**: `on_before_host_create`, `on_host_created`, `on_before_host_destroy`, `on_host_destroyed`

**Agent lifecycle hooks**: `on_before_initial_file_copy`, `on_after_initial_file_copy`, `on_agent_state_dir_created`, `on_before_provisioning`, `on_after_provisioning`, `on_agent_created`, `on_before_agent_destroy`, `on_agent_destroyed`

**Deployment hooks**: `get_files_for_deploy`, `modify_env_vars_for_deploy`

### Maintained Plugins

| Plugin | Package | Description |
|--------|---------|-------------|
| **pair** | `mng-pair` | Continuous bidirectional file sync between local and agent |
| **opencode** | `mng-opencode` | OpenCode agent type support |
| **schedule** | `mng-schedule` | Cron-scheduled recurring agent runs on Modal |
| **kanpan** | `mng-kanpan` | TUI dashboard aggregating agent state, git, GitHub PRs, and CI |
| **tutor** | `mng-tutor` | Interactive tutorials for learning mng |

## Shared Libraries

### libs/imbue_common

Core types and patterns shared across all projects:

- **`primitives.py`** -- constrained types: `NonEmptyStr`, etc.
- **`ids.py`** -- `RandomId` base class for UUID4-based identifiers with type prefixes
- **`frozen_model.py`** -- `FrozenModel(BaseModel)`: immutable Pydantic models with `model_copy_update()`
- **`mutable_model.py`** -- `MutableModel`: for cases where mutation is necessary (used sparingly)
- **`enums.py`** -- `UpperCaseStrEnum` base class
- **`model_update.py`** -- type-safe model update utilities (`to_update_dict()`, `FieldProxy`)

### libs/concurrency_group

Structured management of threads and processes via the `ConcurrencyGroup` context manager. Ensures automatic cleanup, supports nesting, propagates shutdown events, and detects timeouts/failures. Used throughout the codebase for parallel operations (e.g., querying multiple providers simultaneously).

## Security Model

- **Plugins** are fully trusted -- they run with your privileges.
- **Providers** are trusted to enforce isolation and honestly report state.
- **Hosts** provide isolation that depends on the provider (Docker containers, Modal VMs, etc.). Local hosts have no isolation.
- **Agents** on the same host share full access to the host's resources. For isolation, use separate hosts and restrict what information each host receives.

## Design Principles

1. **Direct** -- commands do exactly what you tell them, with minimal magic.
2. **Immediate** -- fast and responsive; minimize wait times.
3. **Safe** -- prioritize safety and reliability; avoid data loss.
4. **Personal** -- serve only the user; no data sharing without explicit permission.

## Style and Conventions

The codebase follows a **stateless, functional, immutable** style:

- Domain-specific types instead of raw primitives (e.g., `AgentName` not `str`)
- `FrozenModel` for data, `MutableModel` only where mutation is required
- `pathlib.Path` for all file paths
- No code in `__init__.py` files
- No `__all__` declarations
- Minimal use of `TYPE_CHECKING` guards
- 80% minimum test coverage, enforced in CI

## Testing

- **Unit tests**: `*_test.py` files (fast, isolated)
- **Integration tests**: `test_*.py` files (no marker)
- **Acceptance tests**: `test_*.py` with `@pytest.mark.acceptance` (run in CI)
- **Release tests**: `test_*.py` with `@pytest.mark.release` (run in CI)

Run all tests: `uv run pytest` from the repo root. Tests run in parallel via `pytest-xdist` with 4 workers.

## Build and Packaging

- **Build backend**: Hatchling
- **Dependency management**: uv (workspace-aware)
- **Linting/formatting**: ruff
- **Import enforcement**: import-linter (layer contracts)
- **Pre-commit hooks**: ruff formatting on commit
