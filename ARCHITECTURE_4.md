## CLI Architecture Overview Report

Based on my thorough exploration of the mng codebase, here's a detailed understanding of the CLI structure and user-facing commands:

---

## CLI Framework and Entry Points

**Framework**: Click (Python CLI framework) with click-option-group extensions
- **Main entry point**: `/home/user/mng/libs/mng/imbue/mng/main.py` registered via `pyproject.toml` as `mng = "imbue.mng.main:cli"`
- **CLI class**: `AliasAwareGroup` - custom click.Group that supports command aliases and intelligent defaults
- **Default command**: When no subcommand is given, "create" is defaulted. Unrecognized subcommands are treated as arguments to "create"

**Plugin Architecture**:
- Uses `pluggy` for plugin management with a singleton pattern (`_plugin_manager_container`)
- Plugins are loaded via setuptools entrypoints under the "mng" group
- Hooks specs defined in `/home/user/mng/libs/mng/imbue/mng/plugins/hookspecs.py`
- Plugin system allows:
  - Custom CLI commands via `register_cli_commands()` hook
  - Custom CLI options via `register_cli_options()` hook
  - Lifecycle hooks for agents, hosts, and commands
  - Configuration modification via `on_load_config()` hook

---

## Available Commands

### Built-in Commands (from `main.py`):

**Agent Management Commands**:
1. **`create`** (alias: `c`) - Create and run an agent in a host (core command, enabled by default)
2. **`destroy`** (alias: `rm`) - Stop an agent and clean up associated resources
3. **`start`** (alias: `s`) - Start a stopped agent
4. **`stop`** - Stop a running agent
5. **`list`** (alias: `ls`) - List active agents
6. **`connect`** (alias: `conn`) - Attach to an agent interactively
7. **`rename`** (alias: `mv`) - Rename an agent
8. **`clone`** - Create a copy of an existing agent
9. **`migrate`** - Move an agent to a different host

**Data Movement Commands**:
10. **`pull`** - Pull data/code from an agent to local machine
11. **`push`** - Push local changes to an agent
12. **`message`** (alias: `msg`) - Send a message to one or more agents
13. **`exec`** (alias: `x`) - Execute shell commands on an agent's host

**Maintenance Commands**:
14. **`cleanup`** (alias: `clean`) - Clean up stopped agents and unused resources
15. **`logs`** - View agent and host logs with follow mode
16. **`gc`** - Garbage collect unused resources
17. **`snapshot`** (alias: `snap`) - Create snapshots of agent/host state
18. **`limit`** (alias: `lim`) - Configure resource limits
19. **`provision`** (alias: `prov`) - Re-run provisioning on an agent

**Configuration & Meta Commands**:
20. **`config`** (alias: `cfg`) - View and edit mng configuration
21. **`plugin`** (alias: `plug`) - Manage mng plugins (enable/disable/list)
22. **`ask`** - Chat with Claude for help using mng
23. **`--version`** - Display CLI version

### Plugin Commands:

**mng_pair** plugin provides:
- **`pair`** - Continuously sync files between local machine and remote agent

**mng_tutor** plugin provides:
- **`tutor`** - Interactive tutorial for learning mng commands

**mng_kanpan** plugin provides:
- **`kanpan`** - Kanban-style board viewer for tracking agent progress

**mng_schedule** plugin provides:
- **`schedule`** - Schedule periodic execution of agents

---

## Key User Workflows (End-to-End Flows)

### 1. **Create and Use a Local Agent**
```
mng create my-task
```
- Creates agent in local provider (default)
- Automatically connects to interactive session
- Agent runs in tmux window

### 2. **Create and Run Remote Agent with Task**
```
mng create --in modal --no-connect -m "Fix all failing tests"
```
- Creates agent on Modal cloud provider
- Sends initial message without connecting
- Agent runs asynchronously

### 3. **Manage Multiple Agents**
```
mng list                              # See all agents
mng message --all -m "Status update"  # Message all agents
mng destroy --all --force             # Clean up all agents
```
- Multi-target support with CEL filtering
- Batch operations across agents

### 4. **Work with Remote Agent Files**
```
mng pair my-agent                     # Continuous sync
# or
mng push my-agent                     # One-time push
mng pull my-agent --sync-mode git     # Pull changes
```

### 5. **Debug and Inspect**
```
mng logs my-agent output.log --follow
mng exec my-agent "git log --oneline"
mng connect my-agent                  # SSH into agent
```

---

## CLI Structure & Command Organization

### Common Options (Applied to All Commands)
Defined via `@add_common_options` decorator in `common_opts.py`:
- `--format` / `--json` / `--jsonl` - Output format control
- `-q, --quiet` - Suppress console output
- `-v, --verbose` - Increase verbosity (DEBUG, TRACE)
- `--log-file` / `--log-commands` / `--log-command-output` / `--log-env-vars` - Logging configuration
- `--context` - Project context directory
- `--plugin` / `--disable-plugin` - Plugin management

### Command Structure Pattern
Each command follows a consistent pattern:
1. **CliOptions class** - Inherits from `CommonCliOptions`, captures all parameters
2. **@click.command** - Decorated click function with option groups
3. **setup_command_context()** - Centralized initialization loading config, plugins, logging
4. **API layer** - Delegates to API functions (e.g., `api.create`, `api.list`)
5. **Output helpers** - Formats output as human, JSON, or JSONL

### Option Groups
Commands organize options using `click_option_group`:
- "Common" - Shared across all commands
- "Target Selection" - Which agents/hosts to operate on
- "Sync Options", "Display Options", etc. - Command-specific groups

---

## TUI Components

The codebase includes two interactive Text User Interfaces built with **urwid** (terminal widget library):

### 1. **mng_tutor TUI** (`/home/user/mng/libs/mng_tutor/imbue/mng_tutor/tui.py`)
- **Purpose**: Interactive tutorial for learning mng commands
- **Components**:
  - `_LessonSelectorState` - Mutable state for lesson selection
  - `_LessonSelectorInputHandler` - Keyboard input handling
  - `run_lesson_selector()` - Lesson selection interface with colored attributes
  - `run_lesson_runner()` - Lesson execution interface
- **Features**:
  - Arrow key navigation
  - Enter key selection
  - 'q' to quit
  - Real-time lesson progress tracking
  - Automatic step completion detection

### 2. **mng_kanpan TUI** (`/home/user/mng/libs/mng_kanpan/imbue/mng_kanpan/tui.py`)
- **Purpose**: Kanban-style board for tracking agent tasks
- **Components**:
  - `_SelectableText` - Custom urwid Text widget that responds to keyboard
  - `_KanpanState` - Mutable state for the board
  - Board sections: PR_MERGED, PR_CLOSED, PR_BEING_REVIEWED, STILL_COOKING, MUTED
- **Features**:
  - Color-coded agent states (running/attention)
  - CI check status display
  - Agent muting functionality
  - Custom keybindings (r=refresh, p=push, d=delete, m=mute)
  - Real-time board updates (10-minute refresh interval)
  - Spinner feedback during operations

### 3. **connect TUI** (`/home/user/mng/cli/connect.py`)
- Interactive agent selector when multiple agents are available
- Uses urwid for keyboard-driven selection
- Filters agents by status and search query

---

## Plugin System Details

### Hook Specifications
Plugins can implement hooks for:
- **Provider registration**: `register_provider_backend()`, `register_agent_type()`
- **Host lifecycle**: `on_before_host_create()`, `on_host_created()`, `on_before_host_destroy()`, `on_host_destroyed()`
- **Agent lifecycle**: `on_before_initial_file_copy()`, `on_after_provisioning()`, `on_agent_created()`, `on_agent_destroyed()`
- **CLI integration**: `register_cli_commands()`, `register_cli_options()`, `override_command_options()`
- **Configuration**: `on_load_config()`
- **Program lifecycle**: `on_startup()`, `on_before_command()`, `on_after_command()`, `on_error()`, `on_shutdown()`
- **Deployment**: `get_files_for_deploy()`, `modify_env_vars_for_deploy()`
- **Create hooks**: `on_before_create()`

### Plugin Registration
Plugins implement hooks using `@hookimpl` decorator:
```python
from imbue.mng import hookimpl

@hookimpl
def register_cli_commands():
    return [my_custom_command]
```

### Plugin Loading Flow
1. Plugin manager created with `create_plugin_manager()`
2. Disabled plugins blocked before entrypoint loading
3. Setuptools entrypoints loaded under "mng" group
4. All registries loaded to make classes available
5. Plugin CLI options applied to all commands
6. Hooks called at appropriate lifecycle points

---

## Output Formatting

Commands support multiple output formats through a consistent API:

**Built-in Formats**:
- **human** - Human-readable text (default)
- **json** - Complete JSON object
- **jsonl** - JSON Lines (one JSON object per line, streaming-friendly)
- **template** - Custom template strings (e.g., `"{agent.name}\t{agent.state}"`)

**Output Helpers** (in `output_helpers.py`):
- `write_human_line()` - Format human text
- `emit_final_json()` - Output JSON
- `emit_event()` - Output JSONL events
- `emit_format_template_lines()` - Apply custom templates
- `render_format_template()` - Template evaluation

---

## Error Handling & Configuration

### Error Management
- Custom error classes in `/imbue/mng/errors.py` (MngError, BaseMngError, UserInputError, etc.)
- On-error hooks in plugin system for centralized error handling
- Error reporting for unexpected exceptions

### Configuration Loading
- Configuration hierarchy: global → project-specific → CLI overrides
- Config loader in `config/loader.py` handles:
  - Plugin loading and disabling
  - Env var expansion
  - Default value application
  - Validation via Pydantic

### Logging Setup
- Loguru integration for structured logging
- Per-command log files in `~/.mng/logs/`
- Configurable verbosity via `-v` flags
- Environment variable logging (security flagged)

---

## Command Execution Flow

1. **Click parses arguments** → creates CliOptions object
2. **setup_command_context()** called:
   - Loads mng configuration
   - Creates MngContext (central context object)
   - Initializes ConcurrencyGroup for process management
   - Applies config defaults and plugin overrides
3. **Plugin hooks**:
   - `on_before_command()` hook called
   - Command options can be overridden via plugins
4. **Command implementation** delegates to API layer
5. **Output formatting** via format parameter
6. **Plugin hooks**:
   - `on_after_command()` on success
   - `on_error()` on exception
7. **Cleanup** via `on_shutdown()` hook

---

## Key Design Patterns

1. **Separation of Concerns**: CLI layer (click) → API layer (imbue.mng.api) → Core logic
2. **Plugin-First Architecture**: Even built-in features designed as pluggable hooks
3. **Configuration-Driven**: Heavy use of config system for runtime behavior
4. **Output Flexibility**: All commands support human/JSON/JSONL/template formats
5. **Immutable Models**: FrozenModel base class for type-safe data structures
6. **Context Objects**: Single MngContext passed through call chain
7. **Lazy Loading**: Plugin manager and agent lists loaded on-demand

This architecture enables extensibility (via plugins), maintainability (clean layering), and user flexibility (multiple output formats, config-driven behavior).