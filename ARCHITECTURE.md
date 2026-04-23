# Architecture Overview

Overview of the `mng` monorepo: structure, core abstractions, design patterns, and how everything fits together.

## What is mng?

`mng` is a CLI tool for creating and managing AI coding agents (Claude Code, Codex, OpenCode, etc.) locally or remotely. Built on standard open-source tools (SSH, git, tmux, Docker) and extensible via plugins.

Key mission: create, deploy, and manage AI coding agents at scale, with automatic cost optimization through idle detection and shutdown.

## Monorepo Structure

All packages belong to a single **uv workspace** (`[tool.uv.workspace] members = ["libs/*", "apps/*"]`). Shared Python namespace: `imbue.*` (e.g., `imbue.mng`, `imbue.imbue_common`, `imbue.changelings`). Dependencies flow apps -> plugins -> core -> common, with no cycles.

```
libs/
  imbue_common/        # Foundation: primitives, models, utilities (no internal deps)
  concurrency_group/   # Foundation: structured thread/process management (depends on: imbue-common)
  mng/                 # Core framework (depends on: imbue-common, concurrency-group)
  mng_pair/            # Plugin: continuous file sync
  mng_opencode/        # Plugin: OpenCode agent type
  mng_schedule/        # Plugin: cron-scheduled agent runs
  mng_kanpan/          # Plugin: TUI agent tracker dashboard
  mng_tutor/           # Plugin: interactive tutorials
  flexmux/             # Independent: FlexLayout tab manager (no internal deps)
apps/
  changelings/         # Experimental autonomous agent scheduler (depends on: mng)
  claude_web_view/     # Independent: web viewer for Claude Code transcripts (no internal deps)
  sculptor_web/        # Web interface for agent management (depends on: mng)
```

## Core Concepts

Three fundamental abstractions: **agents**, **hosts**, and **providers**.

- **Agent**: A process in window 0 of a `mng-<agent_name>` tmux session. Has a name, unique ID, agent type (claude, codex, etc.), working directory, and lifecycle state.

- **Host**: An isolated sandbox where agents run -- the local machine, a Docker container, a Modal sandbox, or any SSH-accessible machine. Multiple agents can share a host. Hosts auto-pause when all their agents are idle.

- **Provider**: A configured endpoint that creates and manages hosts. **Backends** (local, docker, modal, ssh) are stateless factories; **instances** are configured endpoints created from backends (e.g., "my-modal-gpu" using "modal" with specific GPU/memory settings).

### State Model

`mng` stores almost no persistent state. It reconstructs everything from:

1. **Provider queries** -- inspecting Docker labels, Modal tags, local state files, etc.
2. **Host queries** -- checking SSH connectivity, reading agent state from the filesystem
3. **Configuration files** -- settings, enabled plugins, etc.

No database, no state corruption risk, and multiple `mng` instances can manage the same agents simultaneously.

Agent state lives on the host filesystem:
- `$MNG_HOST_DIR/agents/$MNG_AGENT_ID/data.json` -- certified agent metadata (command, permissions, labels, start-on-boot)
- `$MNG_HOST_DIR/activity/` -- activity files for idle detection (mtime-based timestamps per source)
- Cooperative file locking via `fcntl.flock()` prevents race conditions

### Conventions

`mng` uses naming conventions to identify managed resources:

- Tmux sessions: `mng-<agent_name>` (prefix customizable via `MNG_PREFIX`)
- Host state: `$MNG_HOST_DIR/` (default: `~/.mng`)
- IDs: base16-encoded UUID4s with type prefixes (e.g., `agent-abc123...`, `host-def456...`)
- Names: human-readable, letters/numbers/hyphens only

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

Each layer may only import from layers below it. Checked in CI.

### Key Interfaces

All interfaces live in `interfaces/`. Read the source for method-level details.

- **`AgentInterface`** (`agent.py`) -- identity, command assembly, lifecycle state, user interaction (send message, capture pane), activity tracking, provisioning, and destruction.
- **`HostInterface`** (`host.py`) -- host identity and state. Extended by `OnlineHostInterface` for accessible hosts (command execution, file I/O, agent management, provisioning).
- **`ProviderInstanceInterface`** (`provider_instance.py`) -- host lifecycle (create, stop, start, destroy), discovery, snapshots, and volumes.
- **`ProviderBackendInterface`** (`provider_backend.py`) -- stateless factory creating provider instances from config.
- **`Volume`** (`volume.py`) -- persistent storage abstraction with file operations and path scoping. Implementations: ModalVolume, LocalVolume.

### Core Domain Types

The codebase aggressively uses constrained primitive types to encode domain knowledge at the type level:

**ID Types** (inherit from `RandomId` in `imbue_common`):
- `AgentId`, `HostId`, `VolumeId`, `SnapshotId`
- UUID4-based hex strings with type prefixes, validated at construction

**Constrained String Types**:
- `NonEmptyStr`: cannot be empty or whitespace-only
- `AgentName`, `HostName`, `AgentTypeName`: semantic domain names
- `CommandString`, `ProviderInstanceName`, `ProviderBackendName`, `PluginName`

**Enums** (inherit from `UpperCaseStrEnum`):
- `HostState`: BUILDING, STARTING, RUNNING, STOPPING, STOPPED, PAUSED, CRASHED, FAILED, DESTROYED, UNAUTHENTICATED
- `AgentLifecycleState`: STOPPED, RUNNING, WAITING, REPLACED, DONE
- `ActivitySource`: CREATE, BOOT, START, SSH, PROCESS, AGENT, USER
- `WorkDirCopyMode`: COPY, CLONE, WORKTREE

**Model Base Classes** (from `imbue_common`):
- `FrozenModel`: immutable Pydantic models (`frozen=True`), provides `model_copy_update()` for type-safe updates. Used for DTOs, configuration, certified data.
- `MutableModel`: mutable Pydantic models for interface implementations needing internal state. Critical fields like IDs remain `frozen=True`.
- All models use `extra="forbid"` to catch typos and stale fields.

### Configuration

Configuration loads hierarchically. Later sources override earlier ones, with per-key merging for nested objects (scalars: last writer wins; lists: concatenated; dicts: deep-merged):

1. Built-in defaults (hardcoded in `MngConfig`)
2. User config (`~/.mng/profiles/<profile_id>/settings.toml`)
3. Project config (`.mng/settings.toml` at git root or context dir)
4. Local config (`.mng/settings.local.toml` -- gitignored, for personal overrides)
5. Environment variables (`MNG_PREFIX`, `MNG_HOST_DIR`, `MNG_ROOT_NAME`, `MNG_COMMANDS_*`)
6. CLI arguments (highest precedence)

**Key config types** (`config/data_types.py`):

- **`MngConfig`** -- root config: prefix, host dir, agent types, providers, plugins, command defaults, create templates, logging, and more.
- **`AgentTypeConfig`** -- custom or overridden agent type with parent type inheritance, command, CLI args, and permissions.
- **`ProviderInstanceConfig`** -- per-instance settings. Subclasses add backend-specific fields (e.g., Modal adds GPU, CPU, memory, image, volumes).
- **`MngContext`** -- resolved runtime context passed through the application. Combines `MngConfig`, `PluginManager`, profile directory, concurrency group, and flags like `is_interactive` and `is_auto_approve`.

**Environment variable overrides** for command defaults: `MNG_COMMANDS_<COMMAND>_<PARAM>=<value>` (e.g., `MNG_COMMANDS_CREATE_NEW_BRANCH_PREFIX=agent/`).

### CLI Commands

Built with [Click](https://click.palletsprojects.com/) and `click-option-group`. Entry point: `imbue.mng.main:cli`, registered as the `mng` console script.

`AliasAwareGroup` is a custom Click group supporting command aliases; defaults to `create` when no subcommand is given.

**Primary** (agent management): `create` (alias: `c`), `destroy` (alias: `rm`), `connect` (alias: `conn`), `list` (alias: `ls`), `stop`, `start` (alias: `s`), `exec` (alias: `x`), `rename` (alias: `mv`)

**Data transfer**: `pull`, `push`, `pair`, `message` (alias: `msg`)

**Setup**: `provision` (alias: `prov`), `clone`, `migrate`

**Maintenance**: `cleanup` (alias: `clean`), `logs`, `events`, `gc`, `snapshot` (alias: `snap`), `limit` (alias: `lim`)

**Meta**: `config` (alias: `cfg`), `plugin` (alias: `plug`), `ask`

Commands follow a consistent pattern: CliOptions class -> @click.command -> setup_command_context() -> API layer -> output formatting (human/JSON/JSONL/template).

### Event Stream System

Agents and hosts emit structured events to JSONL files under `$MNG_HOST_DIR/logs/`. All events extend `EventEnvelope` (from `imbue_common`), providing timestamp, type, event ID, and source. Streamable and filterable via `mng events` (experimental). The streaming API (`api/events.py`) supports direct host access, volume-based reads for offline hosts, and timestamp-ordered merging across sources with CEL expression filtering.

## Plugin System

`mng` uses [pluggy](https://pluggy.readthedocs.io/) for extensibility. All extensible components -- agent types, provider backends, CLI commands -- register through plugin hooks in two tiers:

**Default plugins** ship inside `libs/mng` (provider backends in `providers/`, agent types in `agents/default_plugins/`) and register via `pm.register()` at startup. The minimal core (layered architecture, interfaces, config, utils, primitives) is independent of these.

**External plugins** (the `mng_*` packages) are separate packages declaring a setuptools entry point:

```toml
# In a plugin's pyproject.toml
[project.entry-points.mng]
my_plugin = "my_package.plugin"
```

### Plugin Manager Lifecycle

At startup, `create_plugin_manager()` loads hookspecs from `plugins/hookspecs.py`, blocks disabled plugins via `pm.set_blocked()`, discovers external plugins via entry points, registers built-in defaults, and populates all registries by calling registration hooks.

### Hooks and Execution Flows

**Registration hooks** (called once at startup):
- `register_agent_type` -- add agent types (returns name, class, config)
- `register_provider_backend` -- add provider backends (returns backend class, config class)
- `register_cli_commands` -- add CLI commands (returns list of Click commands)
- `register_cli_options` -- add options to existing commands (returns option group mapping)

**Program lifecycle hooks** (called in order for every command):
`on_load_config` -> `override_command_options` -> `on_startup` -> `on_before_command` (can abort) -> command executes -> `on_after_command` (or `on_error` on exception) -> `on_shutdown`

**Agent create flow** (hooks fired during `mng create`):
`on_before_create` (chained, can modify args) -> `on_before_host_create` -> provider creates host -> `on_host_created` -> `on_before_initial_file_copy` -> files copied -> `on_after_initial_file_copy` -> `on_agent_state_dir_created` -> `on_before_provisioning` -> agent provisioning -> `on_after_provisioning` -> `on_agent_created`

**Destroy hooks**: `on_before_agent_destroy`, `on_agent_destroyed`, `on_before_host_destroy`, `on_host_destroyed`

**Deployment hooks**: `get_files_for_deploy`, `modify_env_vars_for_deploy`

### Writing a Plugin

See [libs/mng/docs/concepts/plugins.md](libs/mng/docs/concepts/plugins.md#writing-a-plugin) for a full guide covering package setup, hook implementations, plugin configuration, CLI commands, and error handling.

## Shared Libraries

### libs/imbue_common

Foundation types shared across all projects: primitives, IDs, model base classes, and enums (see Core Domain Types), plus `EventEnvelope` (see Event Stream System) and `model_update.py` (type-safe model update utilities).

### libs/concurrency_group

Structured thread/process management via `ConcurrencyGroup` context manager. Automatic cleanup, nesting, shutdown propagation, and timeout/failure detection. Used throughout for parallel operations (e.g., querying multiple providers simultaneously).

## Applications

### apps/changelings

Experimental autonomous agent scheduler. Depends on mng, imbue-common, concurrency-group, and modal. Includes Modal deployment modules (cron_runner, remote_runner, verification).

## Security Model

- **Plugins** are fully trusted -- they run with your privileges.
- **Providers** are trusted to enforce isolation and honestly report state.
- **Hosts** provide isolation depending on the provider (Docker containers, Modal VMs, etc.). Local hosts have none.
- **Agents** on the same host share full access to host resources. For isolation, use separate hosts and restrict what each host receives.

## Design Principles

1. **Direct** -- commands do exactly what you tell them, minimal magic.
2. **Immediate** -- fast and responsive; minimize wait times.
3. **Safe** -- prioritize safety and reliability; avoid data loss.
4. **Personal** -- serve only the user; no data sharing without explicit permission.

## Build and Key Technologies

| Category | Technologies |
|----------|-------------|
| Language | Python 3.11+ |
| Package mgmt | uv (workspace-aware monorepo) |
| Build | Hatchling |
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
| Quality | ruff (line length 119, double quotes), ty (`uv run ty check`), pre-commit |

**GitHub Actions CI** (`.github/workflows/ci.yml`): runs integration, acceptance, and release test suites in parallel. Acceptance tests require Modal credentials. Release tests run on the release branch only.
