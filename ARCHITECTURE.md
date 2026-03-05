# Architecture Overview

This document provides a comprehensive overview of the `mng` monorepo -- its structure, core abstractions, design patterns, and how everything fits together.

## What is mng?

`mng` is a CLI tool for creating and managing AI coding agents (Claude Code, Codex, OpenCode, etc.) that can run locally or remotely. It is built on standard open-source tools (SSH, git, tmux, Docker) and is extensible via a plugin system.

The key mission: make it easy to create, deploy, and manage AI coding agents at scale, whether running locally or on remote platforms, with automatic cost optimization through idle detection and shutdown.

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
    sculptor_web/                # Web interface for agent management (FastHTML)
  scripts/                       # Build and utility scripts
  style_guide.md                 # Coding standards
  CLAUDE.md                      # Instructions for Claude Code agents working on this repo
  pyproject.toml                 # Monorepo config (uv workspace, pytest, ruff, import-linter)
```

All libraries and apps are part of a single **uv workspace** (`[tool.uv.workspace] members = ["libs/*", "apps/*"]`). The shared Python namespace is `imbue.*` (e.g., `imbue.mng`, `imbue.imbue_common`, `imbue.changelings`).

### Package Dependency Graph

```
imbue-common (foundation: primitives, models, utilities)
concurrency-group (foundation: structured thread/process management)
    |
    v
mng (core framework)
    depends on: imbue-common, concurrency-group
    key deps: click, pydantic, pluggy, urwid, modal, docker, pyinfra
    |
    +-- mng-pair (plugin, depends on: mng)
    +-- mng-opencode (plugin, depends on: mng)
    +-- mng-schedule (plugin, depends on: mng, imbue-common, modal)
    +-- mng-kanpan (plugin, depends on: mng)
    +-- mng-tutor (plugin, depends on: mng)
    |
    +-- changelings (app, depends on: mng, imbue-common, concurrency-group, modal)
    +-- sculptor_web (app, depends on: mng, python-fasthtml)

flexmux (independent utility, depends on: flask, pydantic)
claude_web_view (independent app, depends on: fastapi, watchfiles)
```

Dependencies flow cleanly from applications -> plugins -> core framework -> common libraries, with no circular dependencies. This is enforced by import-linter in CI.

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

Agent state lives on the host filesystem:
- `$MNG_HOST_DIR/agents/$MNG_AGENT_ID/data.json` -- certified agent metadata (command, permissions, labels, start-on-boot)
- `$MNG_HOST_DIR/activity/` -- activity files for idle detection (mtime-based timestamps per source)
- Cooperative file locking via `fcntl.flock()` prevents race conditions

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

**`Volume`** (`interfaces/volume.py`) -- persistent storage abstraction:
- File operations: `listdir()`, `read_file()`, `write_files()`, `remove_file()`, `remove_directory()`
- Scoping: `scoped(prefix)` returns a new Volume with path prefix prepended
- Concrete implementations: ModalVolume, LocalVolume (inherit from `BaseVolume`)

### Core Domain Types

The codebase aggressively uses constrained primitive types to encode domain knowledge at the type level:

**ID Types** (all inherit from `RandomId` in `imbue_common`):
- `AgentId`, `HostId`, `VolumeId`, `SnapshotId`
- UUID4-based hex strings with type prefixes, validated at construction time

**Constrained String Types**:
- `NonEmptyStr`: cannot be empty or whitespace-only
- `AgentName`, `HostName`, `AgentTypeName`: semantic domain names
- `CommandString`, `ProviderInstanceName`, `ProviderBackendName`, `PluginName`

**Constrained Numeric Types**:
- `NonNegativeInt`, `PositiveInt`, `NonNegativeFloat`, `PositiveFloat`
- `Probability`: float constrained to [0.0, 1.0]
- `SizeBytes`: non-negative integer for sizes

**Enums** (all inherit from `UpperCaseStrEnum`):
- `HostState`: BUILDING, STARTING, RUNNING, STOPPING, STOPPED, PAUSED, CRASHED, FAILED, DESTROYED, UNAUTHENTICATED
- `AgentLifecycleState`: STOPPED, RUNNING, WAITING, REPLACED, DONE
- `ActivitySource`: CREATE, BOOT, START, SSH, PROCESS, AGENT, USER
- `WorkDirCopyMode`: COPY, CLONE, WORKTREE

**Model Base Classes** (from `imbue_common`):
- `FrozenModel`: immutable Pydantic models with `frozen=True`, provides `model_copy_update()` for type-safe updates. Used for data transfer objects, configuration, certified data.
- `MutableModel`: mutable Pydantic models for interface implementations that need internal state. Critical fields like IDs are still `frozen=True`.
- All models use `extra="forbid"` to catch typos and stale fields.

### Default Plugins vs. Core

The `mng` package itself contains a set of **default plugins** -- provider backends and agent types that are registered internally via `pm.register()` during startup. These are not separate packages; they live inside `libs/mng` and are always available. They use the same hookimpl mechanism as external plugins but are loaded directly rather than discovered via entry points.

**Default provider backends** (in `providers/`):

| Backend | Description | Isolation | Snapshots | Use Case |
|---------|------------|-----------|-----------|----------|
| **local** | Runs on your machine directly | None | No | Quick local dev |
| **docker** | Docker containers | Container-level | No | Local isolation |
| **modal** | Modal.com Sandboxes | VM-level | Yes | Cloud compute, auto-shutdown |
| **ssh** | Any SSH-accessible host | Depends on host | No | Pre-existing infrastructure |

Local and SSH are always loaded. Docker and Modal are conditionally loaded (they can be disabled via `pm.set_blocked()`), though both are hard dependencies of the mng package.

**Default agent types** (in `agents/default_plugins/`):

- **ClaudeAgent** -- Claude Code with configurable model, permissions, and provisioning
- **CodexAgent** -- OpenAI Codex CLI integration
- **SkillAgent** -- Runs a specific skill/script
- **CodeGuardianAgent** -- Code review agent
- **FixmeFairyAgent** -- Automated FIXME resolver

The minimal core of mng (layered architecture, interfaces, config, utils, primitives) is independent of these default plugins. The default plugins provide the concrete implementations that make mng useful out of the box.

Agent types support inheritance: a custom "my-claude" type can inherit from "claude" and merge parent defaults with custom overrides.

### Configuration

Configuration is loaded hierarchically (later sources override earlier):

1. Built-in defaults
2. Global config (`~/.mng/settings.toml`)
3. Local/project config (`.mng/settings.toml`)
4. Environment variables
5. CLI arguments

The `MngContext` dataclass holds the resolved configuration for a given invocation, including the mng data directory, user ID, enabled plugins, and provider instances.

Key config types (`config/data_types.py`):
- `MngConfig`: global settings (default_host_dir, prefix, destroyed_host_persisted_seconds)
- `ProviderInstanceConfig`: per-provider settings (backend name, host_dir, build/start args)
- `AgentTypeConfig`: agent type definitions (command, CLI args, permissions)
- `ActivityConfig`: idle detection settings (idle_mode, idle_timeout_seconds)

### CLI Commands

The CLI is built with [Click](https://click.palletsprojects.com/) and uses `click-option-group` for option organization. The main entry point is `imbue.mng.main:cli`, registered as the `mng` console script.

`AliasAwareGroup` is a custom Click group that supports command aliases and defaults to `create` when no subcommand is given.

**Primary** (agent management): `create` (alias: `c`), `destroy` (alias: `rm`), `connect` (alias: `conn`), `list` (alias: `ls`), `stop`, `start` (alias: `s`), `exec` (alias: `x`), `rename` (alias: `mv`)

**Data transfer**: `pull`, `push`, `pair`, `message` (alias: `msg`)

**Setup**: `provision` (alias: `prov`), `clone`, `migrate`

**Maintenance**: `cleanup` (alias: `clean`), `logs`, `gc`, `snapshot` (alias: `snap`), `limit` (alias: `lim`)

**Meta**: `config` (alias: `cfg`), `plugin` (alias: `plug`), `ask`

Commands follow a consistent pattern: CliOptions class -> @click.command -> setup_command_context() -> API layer delegation -> output formatting (human/JSON/JSONL/template).

### TUI Components

The codebase includes interactive text user interfaces built with **urwid**:

- **mng_kanpan**: Kanban-style board tracking agent tasks with color-coded states, CI check status, custom keybindings (r=refresh, p=push, d=delete, m=mute), and 10-minute auto-refresh
- **mng_tutor**: Interactive lesson selector with arrow key navigation and real-time progress tracking
- **connect**: Interactive agent selector when multiple agents are available

## Plugin System

`mng` uses [pluggy](https://pluggy.readthedocs.io/) for its plugin system. There are two tiers of plugins:

1. **Default plugins** -- ship inside `libs/mng` itself (provider backends like modal/docker, agent types like claude/codex). Registered directly via `pm.register()` during startup. See "Default Plugins vs. Core" above.

2. **External plugins** -- separate packages that declare a setuptools entry point under the `mng` group:

```toml
# In a plugin's pyproject.toml
[project.entry-points.mng]
my_plugin = "my_package.plugin"
```

Both tiers use the same `@hookimpl` mechanism and have access to the same hooks.

### Plugin Manager Lifecycle

1. `create_plugin_manager()` creates `pluggy.PluginManager("mng")`
2. Hookspecs registered from `plugins/hookspecs.py`
3. Disabled plugins blocked via `pm.set_blocked()` (prevents hooks from firing without removing the plugin object, so `mng plugin list` still shows them)
4. External plugins loaded via `pm.load_setuptools_entrypoints("mng")`
5. Default plugins registered via `pm.register()` for built-in agent types and provider backends
6. All registries populated via `load_all_registries(pm)` (calls registration hooks across both tiers)

### Hook Categories

**Registration hooks** (called once at startup):
- `register_agent_type` -- add new agent types (returns name, class, config)
- `register_provider_backend` -- add new provider backends (returns backend class, config class)
- `register_cli_commands` -- add new CLI commands (returns list of Click commands)
- `register_cli_options` -- add options to existing commands (returns option group mapping)

**Program lifecycle hooks** (called during execution):
- `on_startup`, `on_before_command`, `on_after_command`, `on_error`, `on_shutdown`
- `on_load_config` -- modify config dict before validation
- `override_command_options` -- transform parsed args before execution

**Host lifecycle hooks**: `on_before_host_create`, `on_host_created`, `on_before_host_destroy`, `on_host_destroyed`

**Agent lifecycle hooks**: `on_before_initial_file_copy`, `on_after_initial_file_copy`, `on_agent_state_dir_created`, `on_before_provisioning`, `on_after_provisioning`, `on_agent_created`, `on_before_agent_destroy`, `on_agent_destroyed`

**Deployment hooks**: `get_files_for_deploy`, `modify_env_vars_for_deploy`

**Create hooks**: `on_before_create` -- inspect/modify create arguments (chained: each hook receives output from previous)

### Command Execution Flow

1. Click parses arguments -> creates CliOptions object
2. `setup_command_context()` loads config, creates MngContext, initializes ConcurrencyGroup
3. Plugin hooks: `on_before_command()` (can abort)
4. Command delegates to API layer
5. Plugin hooks: `on_after_command()` on success, `on_error()` on exception
6. Cleanup via `on_shutdown()`

### Agent Create Flow

1. `on_before_create()` -- plugins inspect/modify arguments
2. `on_before_host_create()` -> provider creates host -> `on_host_created()`
3. `on_before_initial_file_copy()` -> files copied -> `on_after_initial_file_copy()`
4. `on_agent_state_dir_created()`
5. `on_before_provisioning()` -> agent provisioning (class methods) -> `on_after_provisioning()`
6. `on_agent_created()`

### Maintained Plugins

| Plugin | Package | Description |
|--------|---------|-------------|
| **pair** | `mng-pair` | Continuous bidirectional file sync between local and agent |
| **opencode** | `mng-opencode` | OpenCode agent type support |
| **schedule** | `mng-schedule` | Cron-scheduled recurring agent runs on Modal |
| **kanpan** | `mng-kanpan` | TUI dashboard aggregating agent state, git, GitHub PRs, and CI |
| **tutor** | `mng-tutor` | Interactive tutorials for learning mng |

### Writing a Plugin

**New agent type:**
```python
from imbue.mng import hookimpl
from imbue.mng.config.data_types import AgentTypeConfig

class MyAgentConfig(AgentTypeConfig):
    pass

@hookimpl
def register_agent_type():
    return ("my-agent", None, MyAgentConfig)  # None class = use BaseAgent
```

**New provider backend:**
```python
@hookimpl
def register_provider_backend():
    return (MyProviderBackend, MyProviderConfig)
```

**New CLI command:**
```python
@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    return [my_command]
```

## Shared Libraries

### libs/imbue_common

Core types and patterns shared across all projects:

- **`primitives.py`** -- constrained types: `NonEmptyStr`, `Probability`, `PositiveInt`, etc.
- **`ids.py`** -- `RandomId` base class for UUID4-based identifiers with type prefixes
- **`frozen_model.py`** -- `FrozenModel(BaseModel)`: immutable Pydantic models with `model_copy_update()`
- **`mutable_model.py`** -- `MutableModel`: for cases where mutation is necessary (used sparingly)
- **`enums.py`** -- `UpperCaseStrEnum` base class
- **`model_update.py`** -- type-safe model update utilities (`to_update_dict()`, `FieldProxy`)

### libs/concurrency_group

Structured management of threads and processes via the `ConcurrencyGroup` context manager. Ensures automatic cleanup, supports nesting, propagates shutdown events, and detects timeouts/failures. Used throughout the codebase for parallel operations (e.g., querying multiple providers simultaneously).

## Applications

### apps/changelings

Experimental project for scheduling and running autonomous agents. Depends on mng, imbue-common, concurrency-group, and modal. Includes deployment modules for Modal (cron_runner, remote_runner, verification).

### apps/claude_web_view

Web viewer for Claude Code session transcripts. FastAPI backend with Server-Sent Events (SSE) for live updates. React + TypeScript frontend using Radix UI components.

### apps/sculptor_web

Web interface for managing AI agents programmatically. FastAPI-based with python-fasthtml for server-side rendering.

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

Key fixtures: `temp_host_dir`, `temp_mng_ctx`, `local_provider`, `plugin_manager`, `cg` (ConcurrencyGroup), `mng_test_id`.

Coverage requirements: 80% minimum (hard), 81% warning threshold. Excluded: test files, TUI files, Modal/Docker providers.

## Build and CI/CD

- **Build backend**: Hatchling
- **Dependency management**: uv (workspace-aware)
- **Linting/formatting**: ruff (line length 119, double quotes)
- **Import enforcement**: import-linter (layer contracts)
- **Type checking**: ty, run via `uv run ty check`
- **Pre-commit hooks**: ruff formatting on commit

**GitHub Actions CI** (`.github/workflows/ci.yml`):
- **test-integration**: 4 parallel groups with pytest-split, runs unit + integration tests
- **test-acceptance**: 16 parallel groups, 90-second timeout, requires Modal credentials
- **test-release**: 12 parallel groups on release branch only
- **cleanup-modal-environments**: deletes stale Modal test environments (>1 hour old)

**Local development** via justfile targets: `test-unit`, `test-integration`, `test-quick`, `test-acceptance`, `test-release`, `test <path>`.

## Key Technologies

| Category | Technologies |
|----------|-------------|
| Language | Python 3.11+ |
| Package mgmt | uv (workspace-aware monorepo) |
| CLI | Click, click-option-group |
| Data models | Pydantic |
| Plugins | pluggy |
| TUI | urwid |
| Cloud | Modal, Docker |
| Remote access | SSH, pyinfra |
| Sessions | tmux |
| File sync | rsync, unison |
| Web (apps) | FastAPI, FastHTML, React + TypeScript, Flask |
| Testing | pytest, pytest-xdist, pytest-cov, inline-snapshot |
| Quality | ruff, ty (type checker), import-linter, pre-commit |
