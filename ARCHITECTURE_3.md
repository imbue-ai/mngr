## DETAILED REPORT: PLUGIN SYSTEM AND EXTENSIBILITY ARCHITECTURE

### 1. PLUGIN ARCHITECTURE

The mng system uses **pluggy** (the plugin framework used by pytest) to implement a sophisticated, hook-based plugin architecture. This is a mature, production-tested pattern that allows plugins to extend the system without modifying core code.

**Key Characteristics:**
- **Entry Point Discovery**: External plugins are discovered via setuptools entry points under the `"mng"` group
- **Hook System**: Plugins implement hooks (marked with `@hookimpl`) to register capabilities and respond to lifecycle events
- **Plugin Manager**: A single `pluggy.PluginManager` instance manages all plugin registration, hook invocation, and lifecycle
- **Plugin Discovery Location**: `libs/mng/imbue/mng/main.py` lines 239-288 define the plugin manager lifecycle

**Plugin Manager Initialization Flow** (from `main.py:create_plugin_manager()`):
1. Create `pluggy.PluginManager("mng")`
2. Register hookspecs from `libs/mng/imbue/mng/plugins/hookspecs.py`
3. Block disabled plugins using `pm.set_blocked()` (prevents hooks from firing)
4. Load external plugins via `pm.load_setuptools_entrypoints("mng")`
5. Load all registries via `load_all_registries(pm)` (which calls `load_backends_from_plugins()` and `load_agents_from_plugins()`)

**Plugin Blocking Mechanism** (from `config/loader.py:block_disabled_plugins()`):
- Uses `pm.set_blocked(name)` to prevent disabled plugins' hooks from firing
- Can be called before or after plugin registration
- Idempotent - safe to call multiple times
- Two-phase blocking: config file plugins blocked at startup, CLI-level disabled plugins blocked during config loading

---

### 2. HOOK SPECIFICATIONS

The complete set of hookspecs is defined in `/home/user/mng/libs/mng/imbue/mng/plugins/hookspecs.py` (386 lines). There are 21 hooks organized into these categories:

#### **Registration Hooks** (Called once at startup)

1. **`register_provider_backend()`** (lines 24-36)
   - Returns: `tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]` or `None`
   - Used by: Provider backends to register themselves
   - Example: `docker/backend.py` line 75-78, `local/backend.py` line 70-73, `modal/backend.py`

2. **`register_agent_type()`** (lines 39-48)
   - Returns: `tuple[str, type[AgentInterface]|None, type|None]` or `None`
   - Used by: Agent type plugins to register agent classes and configs
   - Example: `mng_opencode/plugin.py` line 52-55, `claude_agent.py`

3. **`register_cli_commands()`** (lines 210-232)
   - Returns: `Sequence[click.Command]` or `None`
   - Used by: Plugins to add new top-level CLI commands
   - Example: `mng_pair/plugin.py` line 9-12, `mng_schedule/plugin.py` line 11-14, `mng_tutor/plugin.py` line 9-12

4. **`register_cli_options(command_name: str)`** (lines 181-190)
   - Returns: `Mapping[str, list[OptionStackItem]]` or `None`
   - Used by: Plugins to add custom options to existing commands
   - Pattern: Group name → list of OptionStackItem objects
   - Applied in: `main.py:apply_plugin_cli_options()` lines 191-236

#### **Configuration/Customization Hooks**

5. **`on_load_config(config_dict: dict)`** (lines 194-207)
   - Runs when loading configuration before final validation
   - Called from: `config/loader.py` line 183
   - Allows plugins to dynamically modify config before validation

6. **`override_command_options(command_name, command_class, params)`** (lines 236-267)
   - Called after CLI parsing but before options object creation
   - Plugins can modify params dict in place
   - Multiple plugins see changes from previous plugins

7. **`on_before_create(args: OnBeforeCreateArgs)`** (lines 324-348)
   - Allows inspection/modification of create arguments
   - Called at start of create(), before any work
   - Hooks called in chain; each receives output from previous hooks

#### **Deployment Hooks**

8. **`get_files_for_deploy(...)`** (lines 271-302)
   - Collects files to include in deployment images
   - Returns: `dict[Path, Path | str]` mapping destination to source
   - Used by: `mng_schedule` for scheduled command deployments
   - Called from: `schedule/implementations/modal/deploy.py`

9. **`modify_env_vars_for_deploy(mng_ctx, env_vars)`** (lines 306-320)
   - Mutates environment variables dict for deployment
   - Called after initial env vars are assembled
   - Used by: Schedule deployment and Claude agent provisioning

#### **Host Lifecycle Hooks** (called during create/destroy)

10. **`on_before_host_create(name, provider_name)`** (lines 55-62)
    - Fires before provider.create_host() during `mng create`
    - Marked: [experimental]

11. **`on_host_created(host)`** (lines 66-71)
    - Fires after provider.create_host() completes
    - Host is accessible with all infrastructure ready

12. **`on_before_host_destroy(host)`** (lines 75-82)
    - Fires before provider.destroy_host()
    - Host still accessible

13. **`on_host_destroyed(host)`** (lines 86-92)
    - Fires after provider.destroy_host() completes
    - Python object still available for metadata

#### **Agent Lifecycle Hooks**

14. **`on_before_initial_file_copy(agent_options, host)`** (lines 99-104)
    - Before copying files to create work directory

15. **`on_after_initial_file_copy(agent_options, host, work_dir_path)`** (lines 108-115)
    - After copying files

16. **`on_agent_state_dir_created(agent, host)`** (lines 119-124)
    - After state directory and data.json created
    - Before provisioning begins

17. **`on_before_provisioning(agent, host)`** (lines 128-132)
    - Before host.provision_agent() called

18. **`on_after_provisioning(agent, host)`** (lines 136-140)
    - After provisioning completes

19. **`on_agent_created(agent, host)`** (lines 144-150)
    - After agent fully created and started
    - Good for logging, notifications, custom setup

20. **`on_before_agent_destroy(agent, host)`** (lines 154-164)
    - Before online agent is destroyed

21. **`on_agent_destroyed(agent, host)`** (lines 168-177)
    - After online agent destroyed
    - Only for online agents

#### **Program Lifecycle Hooks**

22. **`on_post_install(plugin_name)`** (lines 355-356)
    - [future] After plugin installation/upgrade

23. **`on_startup()`** (lines 360-361)
    - When mng starts, before any command runs
    - Called from: `main.py:cli()` line 158
    - Plugins can register callbacks here

24. **`on_before_command(command_name, command_params)`** (lines 365-370)
    - Before any command executes
    - Can raise to abort execution

25. **`on_after_command(command_name, command_params)`** (lines 374-375)
    - After command succeeds
    - Good for logging/cleanup/post-processing

26. **`on_error(command_name, command_params, error)`** (lines 379-380)
    - When command raises exception
    - Good for error handling/reporting

27. **`on_shutdown()`** (lines 384-385)
    - When mng shuts down
    - Called from: `main.py:cli()` line 159 via `ctx.call_on_close()`

---

### 3. PROVIDER MODEL

Providers enable mng to run agents on different infrastructure types. The provider system has two layers:

#### **Provider Backends** (Factories for provider instances)

Located in: `libs/mng/imbue/mng/interfaces/provider_backend.py` and `libs/mng/imbue/mng/providers/registry.py`

**Interface** (`ProviderBackendInterface`):
- `get_name() -> ProviderBackendName`: Unique identifier
- `get_description() -> str`: Human-readable description
- `get_config_class() -> type[ProviderInstanceConfig]`: Config type for validation
- `get_build_args_help() -> str`: Help text for build flags
- `get_start_args_help() -> str`: Help text for start flags
- `build_provider_instance()`: Creates configured provider instances

**Built-in Backends Registered in `providers/registry.py`** (lines 59-100):
1. **local** - Runs agents directly on your machine (always available)
2. **ssh** - SSH-based host backend (experimental)
3. **docker** - Docker-based containers with SSH access
4. **modal** - Modal cloud platform integration

**Registration Process**:
1. Backends register via `@hookimpl register_provider_backend()` hook
2. `load_backends_from_plugins()` calls `pm.hook.register_provider_backend()`
3. Results stored in `_backend_registry` (maps name to backend class)
4. Results stored in `_config_registry` (maps name to config class)

**Provider Instance Configuration**:
- Base class: `ProviderInstanceConfig` (in `config/data_types.py`)
- Each backend subclasses this with backend-specific options
- Example: `LocalProviderConfig`, `DockerProviderConfig`, `ModalProviderConfig`

**Provider Lazy Loading** (comment in `providers/registry.py` lines 1-9):
- Modal import adds ~0.1s to every command
- Called conditionally based on enabled plugins
- Candidates for optimization: move imports into `load_backends_from_plugins()` or `load_local_backend_only()`

#### **Provider Instances**

Created by backends, instances manage:
- **Host Creation** - Allocate resources, build images, start
- **Host Lifecycle** - Stop, start, destroy
- **Snapshots** - Capture filesystem state (optional)
- **Host Discovery** - List mng-managed hosts
- **State Management** - Store state in provider (not locally in mng)

---

### 4. AGENT TYPE SYSTEM

Similar to providers, agent types use a registration hook pattern.

#### **Agent Registry** (`libs/mng/imbue/mng/agents/agent_registry.py`)

**Two Registries**:
1. `_agent_class_registry`: Maps agent type name → agent class
2. `_agent_config_registry`: Maps agent type name → config class

**Registration Process** (lines 42-62):
1. Built-in agent types registered as modules:
   - `claude_agent` (ClaudeAgent)
   - `codex_agent`
   - `code_guardian_agent`
   - `fixme_fairy_agent`
2. `pm.hook.register_agent_type()` called to get plugin registrations
3. Each registration: `(agent_type_name, agent_class, config_class)`
4. Plugins can register new agent types

**Example Plugin Registration**:
```python
# mng_opencode/plugin.py
@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface]|None, type[AgentTypeConfig]]:
    return ("opencode", None, OpenCodeAgentConfig)
```
- `None` agent_class means use default `BaseAgent`
- Custom config class defines behavior

**Agent Type Resolution** (lines 167-210):
- Supports custom types with parent_type (inheritance)
- Example: custom "my-claude" type can inherit from "claude"
- Merges parent defaults with custom overrides

#### **Agent Provisioning Methods** (not hooks, but class methods):

Each agent class can override:
- `on_before_provisioning()` - Validate preconditions
- `get_provision_file_transfers()` - Files to copy
- `provision()` - Agent-specific setup (install packages, create configs)
- `on_after_provisioning()` - Finalization and verification

---

### 5. EXTENSION POINTS & HOW TO ADD NEW CAPABILITIES

#### **Add a New Agent Type**

1. Create a plugin package with:
   ```python
   from imbue.mng import hookimpl
   from imbue.mng.config.data_types import AgentTypeConfig
   
   class MyAgentConfig(AgentTypeConfig):
       # Custom fields
       pass
   
   @hookimpl
   def register_agent_type():
       return ("my-agent", None, MyAgentConfig)
   ```

2. Register setuptools entry point in `pyproject.toml`:
   ```toml
   [project.entry-points."mng"]
   my-plugin = "my_plugin.plugin"
   ```

3. Install plugin: `uv pip install ./my-plugin` or `mng plugin add --path ./my-plugin`

#### **Add a New Provider Backend**

1. Implement `ProviderBackendInterface`:
   ```python
   class MyProviderBackend(ProviderBackendInterface):
       @staticmethod
       def get_name() -> ProviderBackendName:
           return ProviderBackendName("my-provider")
       
       @staticmethod
       def build_provider_instance(...) -> ProviderInstanceInterface:
           return MyProviderInstance(...)
   ```

2. Register via hookimpl:
   ```python
   @hookimpl
   def register_provider_backend():
       return (MyProviderBackend, MyProviderConfig)
   ```

#### **Add a New CLI Command**

1. Create click command:
   ```python
   @click.command()
   @click.option("--option", help="Help text")
   def my_command(option: str) -> None:
       pass
   ```

2. Register via hookimpl:
   ```python
   @hookimpl
   def register_cli_commands() -> Sequence[click.Command]:
       return [my_command]
   ```

3. Install plugin with setuptools entry point

#### **Add Custom Options to Existing Commands**

1. Implement `register_cli_options`:
   ```python
   @hookimpl
   def register_cli_options(command_name: str):
       if command_name == "create":
           return {
               "My Plugin Options": [
                   OptionStackItem(
                       param_decls=("--my-flag",),
                       type=str,
                       help="..."
                   ),
               ]
           }
       return None
   ```

2. Access in command via `override_command_options()` hook to validate/transform

#### **Provide Files/Config for Deployment**

1. Implement `get_files_for_deploy`:
   ```python
   @hookimpl
   def get_files_for_deploy(mng_ctx, include_user_settings, ...):
       files = {}
       if include_user_settings:
           files[Path("~/.my-config")] = Path.home() / ".my-config"
       return files
   ```

2. Implement `modify_env_vars_for_deploy` to mutate env vars for deployment

---

### 6. PLUGIN LIFECYCLE & EXECUTION FLOW

#### **Startup**
1. `create_plugin_manager()` creates pluggy.PluginManager
2. Hooks from `hookspecs.py` registered
3. Disabled plugins blocked via `pm.set_blocked()`
4. External plugins loaded via setuptools entry points
5. `load_all_registries(pm)` loads agents and provider backends

#### **Command Execution** (from `main.py:AliasAwareGroup.invoke()`)
1. `pm.hook.on_startup()` - plugins initialize
2. Plugin CLI options applied to all commands via `apply_plugin_cli_options()`
3. `pm.hook.on_before_command()` - plugins can abort execution
4. Command executes
5. `pm.hook.on_after_command()` or `pm.hook.on_error()` fires
6. `pm.hook.on_shutdown()` - plugins clean up

#### **During Agent Create** (from `api/create.py`)
1. `pm.hook.on_before_host_create()`
2. Provider creates host
3. `pm.hook.on_host_created()`
4. `pm.hook.on_before_initial_file_copy()`
5. Files copied
6. `pm.hook.on_after_initial_file_copy()`
7. `pm.hook.on_agent_state_dir_created()`
8. `pm.hook.on_before_provisioning()`
9. Agent provisioning (agent class methods, not hooks)
10. `pm.hook.on_after_provisioning()`
11. `pm.hook.on_agent_created()`

---

### 7. TESTING FIXTURES & PLUGIN TESTING UTILITIES

#### **Key Testing Fixtures** (from `conftest.py`)

Located in `libs/mng/imbue/mng/conftest.py` and shared via `imbue.imbue_common.conftest_hooks`:

1. `plugin_manager` - Creates fresh pluggy.PluginManager for each test
2. `lifecycle_tracker` - Test plugin that records hook invocations
3. `cli_runner` - CliRunner for invoking CLI commands
4. `temp_mng_ctx` - Temporary MNG context with isolated home directory
5. `local_provider` - Local provider instance for testing

**Global Test Locking** (lines 102-154):
- Prevents parallel pytest from conflicting
- Uses `/tmp/pytest_global_test_lock`
- Acquired at session start, released at session end

#### **Plugin Testing Utilities** (`libs/mng/imbue/mng/utils/plugin_testing.py`)

Provides helpers for:
- Creating test plugins with specific hook implementations
- Registering/unregistering plugins in tests
- Verifying hook invocation

#### **Example Test Plugin** (from `test_lifecycle_hooks.py`):
```python
class _LifecycleTracker:
    @hookimpl
    def on_startup(self) -> None:
        self.calls.append(("on_startup", {}))
    
    @hookimpl
    def on_before_command(self, command_name: str, command_params: dict) -> None:
        self.calls.append(("on_before_command", {...}))

tracker = _LifecycleTracker()
plugin_manager.register(tracker)
```

---

### 8. BUILT-IN PLUGINS (IN THIS MONOREPO)

Located in subdirectories of `libs/`:

1. **mng_pair** - Data synchronization plugin
   - Registers `pair` command via `register_cli_commands()`
   - File: `libs/mng_pair/imbue/mng_pair/plugin.py`

2. **mng_opencode** - OpenCode agent type
   - Registers `opencode` agent type
   - Custom config: `OpenCodeAgentConfig`
   - File: `libs/mng_opencode/imbue/mng_opencode/plugin.py`

3. **mng_schedule** - Scheduled command execution
   - Registers `schedule` command
   - Implements `get_files_for_deploy()` and `modify_env_vars_for_deploy()`
   - File: `libs/mng_schedule/imbue/mng_schedule/plugin.py`

4. **mng_tutor** - Tutoring/assistance plugin
   - Registers `tutor` command
   - File: `libs/mng_tutor/imbue/mng_tutor/plugin.py`

5. **mng_kanpan** - Kanpan plugin (internal)
   - Registers `kanpan` command
   - Custom plugin config via `register_plugin_config()`
   - File: `libs/mng_kanpan/imbue/mng_kanpan/plugin.py`

6. **Built-in Provider Backends**:
   - Local (always available)
   - Docker
   - Modal
   - SSH

7. **Built-in Agent Types**:
   - claude (ClaudeAgent with custom config)
   - codex
   - code_guardian
   - fixme_fairy

---

### 9. PLUGIN CONFIGURATION & MANAGEMENT

#### **Plugin Config Registry** (`config/plugin_registry.py`)

Maps plugin names to custom config classes:
```python
def get_plugin_config_class(plugin_name: str) -> type[PluginConfig]:
    # Returns registered class or default PluginConfig
```

Example (kanpan plugin):
```python
register_plugin_config("kanpan", KanpanPluginConfig)
```

#### **Plugin Management CLI** (`cli/plugin.py`)

- `mng plugin list` - Show installed plugins
- `mng plugin add <name>` - Install plugin
- `mng plugin remove <name>` - Uninstall plugin
- `mng plugin enable <name>` - Enable plugin
- `mng plugin disable <name>` - Disable plugin

#### **Plugin Enabling/Disabling Mechanism**

**Config File (TOML)**:
```toml
[plugins.modal]
enabled = false

[plugins.my-plugin]
enabled = true
```

**CLI Flags**:
```bash
mng create --plugin modal --disable-plugin docker ...
mng create --disable-plugin modal ...
```

**Phase 1 - Startup**: Config file disabled plugins blocked via `pm.set_blocked()` before entry points loaded
**Phase 2 - Config Loading**: CLI-level disabled plugins blocked during `load_config()`

---

### 10. KEY ARCHITECTURAL PATTERNS

#### **Hook-Based Extension**
- Non-invasive: plugins add functionality without modifying core code
- Composable: multiple plugins can implement same hook
- Ordered: hooks fire in registration order

#### **Registry Pattern**
- Lazy loading: registries populated at startup, not at import time
- Type-safe: typed config classes for each agent/provider/plugin
- Extensible: new types registered via hooks, not hardcoded

#### **Two-Phase Configuration**
1. File-based: Static config from TOML
2. Plugin-based: Dynamic config via hooks (on_load_config, override_command_options)

#### **Blocking vs. Unregistering**
- `pm.set_blocked()` prevents execution without removing plugin object
- Allows `mng plugin list` to show disabled plugins
- Safe to call multiple times

#### **Hook Chaining**
- Some hooks receive modified output from previous plugins
- Example: `on_before_create()` returns modified args or None
- Enables composition of plugin behaviors

---

This comprehensive plugin system makes mng highly extensible while maintaining clean separation of concerns and avoiding the common "plugin hell" patterns seen in other systems.