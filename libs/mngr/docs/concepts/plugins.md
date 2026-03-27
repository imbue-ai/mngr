# Plugins

Plugins extend `mngr` with new agent types, providers, commands, and behaviors. They're Python packages using the [pluggy](https://pluggy.readthedocs.io/) framework.

## Managing Plugins

Only install plugins from sources you trust. Built-in plugins are maintained as part of mngr itself.

```bash
mngr plugin list                           # Show installed plugins
mngr plugin add imbue-mngr-opencode              # Install from PyPI
mngr plugin add --path ./my-plugin         # Install from local path
mngr plugin add --git https://github.com/user/repo.git  # Install from git
mngr plugin remove imbue-mngr-opencode           # Uninstall by name
mngr plugin remove --path ./my-plugin      # Uninstall by local path
```

Plugins can be enabled/disabled without uninstalling:

```bash
mngr plugin enable modal             # Enable a plugin
mngr plugin disable modal            # Disable a plugin
mngr plugin disable modal --scope user  # Disable at user scope

# Or disable for a single command
mngr create --disable-plugin modal ...
```

## Hooks

Plugins implement hooks to extend mngr. There are two kinds: registration hooks (which plugins implement to add new capabilities) and lifecycle hooks (which mngr calls on your plugin at specific points during execution). See also [the API reference](./api.md) for a concise summary.

### Registration hooks

Plugins implement these to register new capabilities with mngr. They are called once at startup.

| Hook                         | Description                                                                                                    |
|------------------------------|----------------------------------------------------------------------------------------------------------------|
| `register_agent_type`        | Register a new agent type (e.g., `claude`, `codex`, `opencode`)                                                |
| `register_provider_backend`  | Register a new provider backend (e.g., cloud platforms)                                                        |
| `register_cli_commands`      | Define an entirely new CLI command                                                                             |
| `register_cli_options`       | Add custom CLI options to any existing command's schema so that they appear in `--help`                        |

### Deployment hooks

Called to collect files for baking into deployed images (ex: if you're scheduled a `mngr` command to run at a later point in time or creating a deployed service or website that should be able to call `mngr`). Similar to provisioning, but for environments that *will* do provisioning:

| Hook                         | Description                                                                                                    |
|------------------------------|----------------------------------------------------------------------------------------------------------------|
| `get_files_for_deploy`       | Return files to include in deployment images (e.g., config files, settings). Paths starting with `~` go to the user's home directory; relative paths go to the project working directory. |
| `modify_env_vars_for_deploy` | Mutate the environment variables dict for deployment. Plugins can add, update, or remove env vars in place. Called after env vars are assembled from `--pass-env` and `--env-file` sources. |

### Program lifecycle hooks

mngr calls these at various points in the execution of any command:

| Hook                       | Description                                                                                                                                             |
|----------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------|
| `on_post_install`          | Runs after the plugin is installed or upgraded. Good for setup tasks like prompting the user or downloading models. [future]                            |
| `on_load_config`           | Runs when loading the global config. Receives the current config dict and can modify it before use.                                                     |
| `on_validate_permissions`  | Runs when validating permissions. Should ensure that the correct environment variables and files are accessible. [future]                               |
| `on_startup`               | Runs when `mngr` starts up, before any command runs. Good for registering other callbacks. See [the `mngr` API](./api.md) for more details on registration hooks. [experimental] |
| `on_before_command`        | Runs before any command executes. Receives the command name and resolved parameters dict. Plugins can raise to abort execution. [experimental]          |
| `on_after_command`         | Runs after a command completes successfully. Receives the command name and parameters dict. Useful for logging, cleanup, or post-processing. [experimental] |
| `override_command_options` | Called after argument parsing. Receives the command name and parsed args. Use this to validate or transform extended arguments before the command runs.  |
| `on_error`                 | Runs if any command raises an exception. Receives the command name, parameters dict, and exception. Good for custom error handling or reporting. [experimental] |
| `on_shutdown`              | Runs when `mngr` is shutting down. Good for cleaning up global state or resources. [experimental]                                                       |

Some commands expose additional hooks for finer-grained control. See the documentation of each command for details.

### Host lifecycle hooks

Called during `mngr create` and `mngr destroy` operations:

| Hook                          | Description                                                                                       |
|-------------------------------|---------------------------------------------------------------------------------------------------|
| `on_before_host_create`       | Before creating a new host (receives host name and provider name). [experimental]                 |
| `on_host_created`             | After a new host has been created via provider.create_host().                                     |
| `on_before_host_destroy`      | Before destroying a host via provider.destroy_host(). [experimental]                              |
| `on_host_destroyed`           | After a host has been destroyed. The Python object is still available for metadata. [experimental] |

The following host lifecycle hooks are planned but not yet implemented:

| Hook                          | Description                                                                         |
|-------------------------------|-------------------------------------------------------------------------------------|
| `on_host_collected`           | Called once per host (per command where we collect this host.) [future]             |
| `on_before_machine_create`    | Before creating the underlying environment (machine, container, sandbox) for a host [future] |
| `on_after_machine_create`     | After creating the underlying environment (machine, container, sandbox) for a host [future]  |
| `on_host_state_dir_created`   | When creating the host's state directory [future]                                   |
| `on_before_apply_permissions` | Before applying permissions to a host [future]                                      |
| `on_after_apply_permissions`  | After applying permissions to a host [future]                                       |
| `get_offline_agent_state`     | Use this to provide state for an offline agent [future]                              |

Note that we cannot have callbacks for most host lifecycle events because they can happen outside the control of `mngr`. To implement such functionality, you should provision shell scripts into the appropriate location:

- `$MNGR_HOST_DIR/hooks/boot/`: runs when the host is booted. Blocks service startup until complete.
- `$MNGR_HOST_DIR/hooks/post_services/`: runs after services have been started. Blocks agent startup until complete.
- `$MNGR_HOST_DIR/hooks/stop/`: runs when the host is stopped. Blocks stopping until complete.

### Agent lifecycle hooks

Called during `mngr create` and `mngr destroy` operations:

| Hook                          | Description                                                                                                          |
|-------------------------------|----------------------------------------------------------------------------------------------------------------------|
| `on_before_initial_file_copy` | Before copying files to create the agent's work directory. [experimental]                                            |
| `on_after_initial_file_copy`  | After copying files to create the agent's work directory. [experimental]                                             |
| `on_agent_state_dir_created`  | After the agent's state directory and data.json have been created, before provisioning. [experimental]               |
| `on_before_provisioning`      | Before provisioning an agent (plugin hook, distinct from agent method). [experimental]                               |
| `on_after_provisioning`       | After provisioning an agent (plugin hook, distinct from agent method). [experimental]                                |
| `on_agent_created`            | After an agent is fully created and started.                                                                         |
| `on_before_agent_destroy`     | Before an online agent is destroyed. Does not fire for offline host destruction. [experimental]                      |
| `on_agent_destroyed`          | After an online agent has been destroyed. The Python object is still available for metadata. [experimental]          |

The following agent lifecycle hooks are planned but not yet implemented:

| Hook                                | Description                                                                                           |
|-------------------------------------|-------------------------------------------------------------------------------------------------------|
| `on_agent_collected`                | Called once per agent (per command where we collect this agent.) [future]                             |
| `on_before_apply_agent_permissions` | Before applying permissions to an agent [future]                                                      |
| `on_after_apply_agent_permissions`  | After applying permissions to an agent [future]                                                       |

### Agent Provisioning Methods

Agent provisioning is handled through methods on the agent class itself, not hooks. This allows agent types to define their own provisioning behavior through inheritance:

| Method                        | Description                                                                                           |
|-------------------------------|-------------------------------------------------------------------------------------------------------|
| `on_before_provisioning()`    | Called before provisioning. Validate preconditions (env vars, required files). Raise on failure.      |
| `get_provision_file_transfers()` | Return file transfer specs (local_path, remote_path, is_required) for files to copy during provision. |
| `provision()`                 | Perform agent-type-specific provisioning (install packages, create configs, etc.)                     |
| `on_after_provisioning()`     | Called after all provisioning completes. Finalization and verification.                               |

To customize provisioning for a new agent type, subclass `BaseAgent` and override these methods. The `ClaudeAgent` class demonstrates this pattern.

If you want to run scripts *whenever* an agent is started (not just the first time), you can put a script in the following hook directory [future]:

- `$MNGR_AGENT_STATE_DIR/hooks/start/`: runs after an agent is started. Does not block in any way.

### Field Hooks

Called when collecting data for hosts and agents. These allow plugins to compute additional attributes:

| Hook                       | Description                                                                                                                                     |
|----------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------|
| `host_field_generators` | Return functions for computing additional fields for hosts (and their dependencies). Fields are namespaced under `host.plugin.<plugin_name>`. [future]  |
| `agent_field_generators`   | Return functions for computing additional fields for agents (and their dependencies [future]). Fields are namespaced under `plugin.<plugin_name>`. [experimental]                 |

**Dependency ordering [future]:** The return types for the above hooks are complex: they should return structured types that express both the way of calculating the fields, and the dependencies for those calculations. This allows plugin A's fields to depend on values computed by plugin B. Currently, field generators receive the agent and host objects directly without dependency support.

## Writing a Plugin

A plugin is a Python package that declares an entry point for the `mngr` group and contains functions decorated with `@hookimpl`.

### Package setup

Register your plugin via a setuptools entry point in `pyproject.toml`:

```toml
[project.entry-points.mngr]
my_plugin = "my_package.plugin_module"
```

The entry point name (here `my_plugin`) becomes your plugin's identity -- it appears in `mngr plugin list` and is the name users pass to `mngr plugin enable my_plugin`. The value points to any Python module containing your `@hookimpl` functions.

### Hook implementations

Decorate module-level functions with `@hookimpl` to implement hooks:

```python
from imbue.mngr import hookimpl

@hookimpl
def on_after_provisioning(agent, host, mngr_ctx):
    # your logic here
    ...
```

Hooks must be module-level functions (not methods on a class). The function name must match the hook name exactly. You only need to implement the hooks you care about.

### Plugin configuration

If your plugin needs user-configurable settings, define a config class and register it at import time:

```python
from pydantic import Field
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.config.plugin_registry import register_plugin_config

class MyPluginConfig(PluginConfig):
    some_option: str = Field(default="default_value")

    def merge_with(self, override: "PluginConfig") -> "MyPluginConfig":
        if not isinstance(override, MyPluginConfig):
            return self
        return MyPluginConfig(
            enabled=override.enabled if override.enabled is not None else self.enabled,
            some_option=override.some_option if override.some_option is not None else self.some_option,
        )

# This MUST be at module level (not inside a hook) -- the config system
# needs to know the class before it parses TOML files.
register_plugin_config("my_plugin", MyPluginConfig)
```

The base `PluginConfig` provides an `enabled: bool` field automatically. Custom config classes must implement `merge_with()` so that config layering (user, project, profile) works correctly.

Users configure your plugin in their TOML settings:

```toml
[plugins.my_plugin]
enabled = true
some_option = "custom_value"
```

To access your config at runtime, use `mngr_ctx.get_plugin_config()`:

```python
config = mngr_ctx.get_plugin_config("my_plugin", MyPluginConfig)
# Returns MyPluginConfig with defaults if no config entry exists.
# Raises ConfigParseError if the entry exists but has the wrong type.
```

### How hooks receive state

Hooks receive context through their function arguments. The hookspecs define what each hook gets. There are three patterns:

**Return value hooks** receive nothing and return data. Used by registration hooks:

```python
@hookimpl
def register_cli_commands():
    return [my_command]
```

**Mutable dict hooks** receive a dict and modify it in place. Used by `on_load_config`, `override_command_options`, and `modify_env_vars_for_deploy`:

```python
@hookimpl
def override_command_options(command_name, command_class, params):
    if command_name != "create":
        return
    existing = params.get("extra_window", ())
    params["extra_window"] = (*existing, 'my_window="my-command"')
```

**Chained hooks** receive the previous hook's output and return a modified copy (or `None` to pass through). Used by `on_before_create`:

```python
@hookimpl
def on_before_create(args):
    # Return modified args, or None to pass through unchanged
    return args.model_copy(update={"create_work_dir": False})
```

The `MngrContext` object is the central state carrier. Hooks that receive it can access `mngr_ctx.config` (the merged config), `mngr_ctx.pm` (the plugin manager), and `mngr_ctx.profile_dir` (user profile directory).

### CLI commands

To add a new top-level command to `mngr`, implement `register_cli_commands` and follow the standard command pattern:

```python
# cli.py
import click
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option


class MyCommandOptions(CommonCliOptions):
    my_arg: str | None


@click.command()
@click.argument("my_arg", default=None, required=False)
@add_common_options
@click.pass_context
def my_command(ctx, **kwargs):
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx, command_name="my-command", command_class=MyCommandOptions,
    )
    # opts.my_arg is now typed and available
    ...


CommandHelpMetadata(
    key="my-command",
    one_line_description="Brief description of what this does",
    synopsis="mngr my-command [MY_ARG]",
    description="Extended description.",
).register()

add_pager_help_option(my_command)
```

```python
# plugin.py
from imbue.mngr import hookimpl
from my_package.cli import my_command

@hookimpl
def register_cli_commands():
    return [my_command]
```

### Extending existing commands

Use `override_command_options` to modify the behavior of existing commands. Convention: always check `command_name` first and return early for commands you don't care about. When appending to tuple-valued params, preserve existing values:

```python
@hookimpl
def override_command_options(command_name, command_class, params):
    if command_name != "create":
        return
    existing = params.get("env", ())
    params["env"] = (*existing, "MY_VAR=1")
```

To add visible CLI options to existing commands (so they appear in `--help`), implement `register_cli_options`.

### Error handling

Raise `MngrError` (from `imbue.mngr.errors`) for operational errors that should abort the current command with a user-facing message. Lifecycle hooks that can abort operations (like `on_before_host_create`) do so by raising exceptions.

### Cross-plugin dependencies

If your plugin depends on another plugin, declare it as a standard Python package dependency. You can then import from that plugin's modules directly.

## Built-in Plugins

`mngr` ships with built-in plugins for common agent types:

- **claude**: Claude Code with default configuration
- **codex**: OpenAI Codex integration

And for the basic provider backends:

- **local**: Local host backend
- **docker**: Docker-based host backend
- **modal**: Modal cloud host backend
- **ssh**: SSH-based host backend (connects to pre-configured hosts) [experimental]

Utility plugins [future] for additional features:

- **[local_port_forwarding_via_frp_and_nginx](../core_plugins/local_port_forwarding_via_frp_and_nginx.md)**: Expose services via frp and nginx
- **[default_url_for_cli_agents_via_ttyd](../core_plugins/default_url_for_cli_agents_via_ttyd.md)**: Web terminal access via ttyd
- **[user_activity_tracking_via_web](../core_plugins/user_activity_tracking_via_web.md)**: Track user activity in web interfaces
- **recursive_modal**: Allow recursive invocations of modal agents
- **recursive_mngr**: Allow invocation of mngr itself from within an agent
- **[offline_mngr_state](../core_plugins/offline_mngr_state.md)**: Cache mngr state for use when a host is offline
- **chat_history**: Persistent, globally accessible chat history

These are enabled by default but can be disabled like any other plugin.

## Per-Agent Plugin Data

Plugins can store per-agent data under `$MNGR_AGENT_STATE_DIR/plugin/<plugin_name>/`. Use the agent interface methods:

- `agent.get_reported_plugin_file(plugin_name, filename)` -- read a file
- `agent.set_reported_plugin_file(plugin_name, filename, data)` -- write a file
- `agent.list_reported_plugin_files(plugin_name)` -- list files
- `agent.get_plugin_data(plugin_name)` / `agent.set_plugin_data(plugin_name, data)` -- read/write structured JSON stored in the agent's `data.json`

## Plugin Dependencies

Plugins are Python packages and use standard dependency management. A plugin can depend on other plugins by listing them as package dependencies.
