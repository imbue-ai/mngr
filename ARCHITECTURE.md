# Architecture Overview

This document provides a comprehensive overview of the `mng` monorepo -- its structure, core abstractions, design patterns, and how everything fits together.

## What is mng?

`mng` is a CLI tool for creating and managing AI coding agents (Claude Code, Codex, OpenCode, etc.) that can run locally or remotely. It is built on standard open-source tools (SSH, git, tmux, Docker) and is extensible via a plugin system.

The key mission: make it easy to create, deploy, and manage AI coding agents at scale, whether running locally or on remote platforms, with automatic cost optimization through idle detection and shutdown.

## Monorepo Structure

All packages are part of a single **uv workspace** (`[tool.uv.workspace] members = ["libs/*", "apps/*"]`). The shared Python namespace is `imbue.*` (e.g., `imbue.mng`, `imbue.imbue_common`, `imbue.changelings`). Dependencies flow from apps -> plugins -> core -> common, with no circular dependencies (enforced by import-linter).

```
libs/
  imbue_common/        # Foundation: primitives, models, utilities (no internal deps)
  concurrency_group/   # Foundation: structured thread/process management (no internal deps)
  mng/                 # Core framework (depends on: imbue-common, concurrency-group)
  mng_pair/            # Plugin: continuous file sync (depends on: mng)
  mng_opencode/        # Plugin: OpenCode agent type (depends on: mng)
  mng_schedule/        # Plugin: cron-scheduled agent runs (depends on: mng, imbue-common)
  mng_kanpan/          # Plugin: TUI agent tracker dashboard (depends on: mng)
  mng_tutor/           # Plugin: interactive tutorials (depends on: mng)
  flexmux/             # Independent: FlexLayout tab manager (no internal deps)
apps/
  changelings/         # Experimental autonomous agent scheduler (depends on: mng, imbue-common, concurrency-group)
  claude_web_view/     # Independent: web viewer for Claude Code transcripts (no internal deps)
  sculptor_web/        # Web interface for agent management (depends on: mng)
```

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

### Configuration

Configuration is loaded hierarchically. Later sources override earlier ones, with per-key merging for nested config objects (scalars: last writer wins; lists: concatenated; dicts: deep-merged):

1. Built-in defaults (hardcoded in `MngConfig`)
2. User config (`~/.mng/profiles/<profile_id>/settings.toml`)
3. Project config (`.mng/settings.toml` at git root or context dir)
4. Local config (`.mng/settings.local.toml` -- gitignored, for personal overrides)
5. Environment variables (`MNG_PREFIX`, `MNG_HOST_DIR`, `MNG_ROOT_NAME`, `MNG_COMMANDS_*`)
6. CLI arguments (highest precedence)

The `on_load_config` plugin hook can modify the raw config dict before validation, allowing plugins to inject defaults or transform config.

**Key config types** (`config/data_types.py`):

- **`MngConfig`** -- root configuration model. Key fields:
  - `prefix`: resource naming prefix (default: `"mng-"`)
  - `default_host_dir`: base directory for mng data (default: `~/.mng`)
  - `agent_types`: custom agent type definitions (map of name -> `AgentTypeConfig`)
  - `providers`: provider instance definitions (map of name -> `ProviderInstanceConfig`)
  - `plugins`: plugin configurations (map of name -> `PluginConfig`)
  - `commands`: default CLI parameter values per command (map of command name -> `CommandDefaults`)
  - `create_templates`: named presets for the create command (map of name -> `CreateTemplate`)
  - `pre_command_scripts`: shell commands to run before CLI commands
  - `logging`: file/console log levels, log rotation settings
  - `enabled_backends`: whitelist of provider backends (empty = all enabled)
  - `connect_command`: custom command for agent connection (overrides built-in tmux attach)

- **`AgentTypeConfig`** -- defines a custom or overridden agent type:
  - `parent_type`: base type to inherit from (enables type inheritance)
  - `command`: command to run for this agent type
  - `cli_args`: additional CLI arguments (merged via concatenation)
  - `permissions`: explicit permission list (replaces parent's permissions)

- **`ProviderInstanceConfig`** -- per-provider instance settings:
  - `backend`: which provider backend to use
  - `is_enabled`: toggle without removing config
  - Subclasses add backend-specific fields (e.g., Modal adds `gpu`, `cpu`, `memory`, `image`, `volumes`)

- **`MngContext`** -- the resolved runtime context passed through the application. Combines `MngConfig`, `PluginManager`, profile directory, concurrency group, and flags like `is_interactive` and `is_auto_approve`.

**Environment variable overrides** for command defaults use the pattern `MNG_COMMANDS_<COMMAND>_<PARAM>=<value>` (e.g., `MNG_COMMANDS_CREATE_NEW_BRANCH_PREFIX=agent/`).

**`MNG_ROOT_NAME`** (default: `"mng"`) controls the config file directory name (`.mng/`) and default prefix (`mng-`). This allows multiple independent mng installations on the same machine.

### CLI Commands

The CLI is built with [Click](https://click.palletsprojects.com/) and uses `click-option-group` for option organization. The main entry point is `imbue.mng.main:cli`, registered as the `mng` console script.

`AliasAwareGroup` is a custom Click group that supports command aliases and defaults to `create` when no subcommand is given.

**Primary** (agent management): `create` (alias: `c`), `destroy` (alias: `rm`), `connect` (alias: `conn`), `list` (alias: `ls`), `stop`, `start` (alias: `s`), `exec` (alias: `x`), `rename` (alias: `mv`)

**Data transfer**: `pull`, `push`, `pair`, `message` (alias: `msg`)

**Setup**: `provision` (alias: `prov`), `clone`, `migrate`

**Maintenance**: `cleanup` (alias: `clean`), `logs`, `events`, `gc`, `snapshot` (alias: `snap`), `limit` (alias: `lim`)

**Meta**: `config` (alias: `cfg`), `plugin` (alias: `plug`), `ask`

Commands follow a consistent pattern: CliOptions class -> @click.command -> setup_command_context() -> API layer delegation -> output formatting (human/JSON/JSONL/template).

### Event Stream System

`mng` has a structured event logging and streaming subsystem for observability of agent and host activity.

**Foundation** (`imbue_common/event_envelope.py`): `EventEnvelope` is a shared base class (in `imbue_common`) for all structured event records. Every event written to a `logs/<source>/events.jsonl` file includes envelope fields: `timestamp` (ISO 8601 with nanosecond precision), `type`, `event_id`, and `source`. Subclasses add domain-specific fields. `LogEvent` extends this for diagnostic logging from both Python and bash scripts.

**Event sources on the host**: Agents and hosts emit events to JSONL files under `$MNG_HOST_DIR/logs/<source>/events.jsonl`. Sources are discovered by scanning subdirectories. Files support rotation (`events.jsonl.1`, `events.jsonl.2`, etc.). A `stream_transcript.sh` script streams Claude session transcripts into this format with crash-recovery via UUID-based offset reconciliation.

**Discovery events** (`api/discovery_events.py`): Structured events for host/agent discovery state changes (`AGENT_DISCOVERED`, `HOST_DISCOVERED`, `AGENT_DESTROYED`, `HOST_DESTROYED`, `DISCOVERY_FULL`). These extend `EventEnvelope` and are written during the discovery process.

**Streaming API** (`api/events.py`): `EventsTarget` resolves an agent or host to its event sources, supporting three access strategies: direct host access (SSH), volume-based reads (for offline hosts with Modal volumes), and polling for online/offline transitions. `stream_all_events()` merges events from multiple sources in timestamp order with optional CEL expression filtering.

**CLI** (`cli/events.py`): `mng events` (experimental) streams events in real-time (`--follow`) or historical, with CEL-based `--filter`, `--head`/`--tail` pagination, and source selection.

### TUI Components

The codebase includes interactive text user interfaces built with **urwid**:

- **mng_kanpan**: Kanban-style board tracking agent tasks with color-coded states, CI check status, custom keybindings (r=refresh, p=push, d=delete, m=mute), and 10-minute auto-refresh
- **mng_tutor**: Interactive lesson selector with arrow key navigation and real-time progress tracking
- **connect**: Interactive agent selector when multiple agents are available

## Plugin System

`mng` uses [pluggy](https://pluggy.readthedocs.io/) for extensibility. All extensible components -- agent types, provider backends, CLI commands -- are registered through the same plugin hook mechanism, but there are two tiers:

**Default plugins** ship inside `libs/mng` and are registered directly via `pm.register()` during startup. They are always available and don't require separate installation:

- **Provider backends** (`providers/`): local, ssh, docker, modal
- **Agent types** (`agents/default_plugins/`): ClaudeAgent, CodexAgent, SkillAgent, CodeGuardianAgent, FixmeFairyAgent

The minimal core (layered architecture, interfaces, config, utils, primitives) is independent of these default plugins. The default plugins provide the concrete implementations that make mng useful out of the box.

**External plugins** are separate packages that declare a setuptools entry point:

```toml
# In a plugin's pyproject.toml
[project.entry-points.mng]
my_plugin = "my_package.plugin"
```

| Plugin | Package | Description |
|--------|---------|-------------|
| **pair** | `mng-pair` | Continuous bidirectional file sync between local and agent |
| **opencode** | `mng-opencode` | OpenCode agent type support |
| **schedule** | `mng-schedule` | Cron-scheduled recurring agent runs on Modal |
| **kanpan** | `mng-kanpan` | TUI dashboard aggregating agent state, git, GitHub PRs, and CI |
| **tutor** | `mng-tutor` | Interactive tutorials for learning mng |

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
- **`event_envelope.py`** -- `EventEnvelope` base class for structured event log records (see Event Stream System)
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

- `FrozenModel` for data, `MutableModel` only where mutation is required
- `pathlib.Path` for all file paths

## Build and CI/CD

- **Build backend**: Hatchling
- **Linting/formatting**: ruff (line length 119, double quotes)
- **Import enforcement**: import-linter (layer contracts)
- **Type checking**: ty, run via `uv run ty check`

**GitHub Actions CI** (`.github/workflows/ci.yml`):
- **test-integration**: 4 parallel groups with pytest-split, runs unit + integration tests
- **test-acceptance**: 16 parallel groups, 90-second timeout, requires Modal credentials
- **test-release**: 12 parallel groups on release branch only
- **cleanup-modal-environments**: deletes stale Modal test environments (>1 hour old)

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
