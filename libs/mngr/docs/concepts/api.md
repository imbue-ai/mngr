# mngr Plugin API

Plugins interact with mngr by implementing hooks (defined in `hookspecs.py`). There are two kinds:

## Registration hooks

Plugins implement these to register new capabilities with mngr. They are called once at startup.

| Hook                         | Description                                                                                                    |
|------------------------------|----------------------------------------------------------------------------------------------------------------|
| `register_agent_type`        | Register a new agent type (e.g., `claude`, `codex`, `opencode`)                                                |
| `register_provider_backend`  | Register a new provider backend (e.g., cloud platforms)                                                        |
| `register_cli_commands`      | Define an entirely new CLI command                                                                             |
| `register_cli_options`       | Add custom CLI options to any existing command's schema so that they appear in `--help`                        |

## Lifecycle hooks

mngr calls these on your plugin at specific points during command execution. Implement them to react to events or modify behavior.

| Hook                         | Description                                                                                                    |
|------------------------------|----------------------------------------------------------------------------------------------------------------|
| `on_load_config`             | Modify the configuration dict before final validation                                                          |
| `override_command_options`   | Override or modify command options after CLI parsing and config defaults, but before the command options object is created |
| `on_before_create`           | Inspect and modify create arguments before any work is done                                                    |
| `on_before_host_create`      | React before a new host is created [experimental]                                                              |
| `on_host_created`            | React after a new host has been created                                                                        |
| `on_agent_created`           | React after an agent is fully created and started                                                              |
| `on_before_agent_destroy`    | React before an online agent is destroyed [experimental]                                                       |
| `on_agent_destroyed`         | React after an online agent has been destroyed [experimental]                                                  |
| `on_before_host_destroy`     | React before a host is destroyed [experimental]                                                                |
| `on_host_destroyed`          | React after a host has been destroyed [experimental]                                                           |
