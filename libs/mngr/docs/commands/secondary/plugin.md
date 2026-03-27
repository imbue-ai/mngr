<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr plugin

**Synopsis:**

```text
mngr [plugin|plug] <subcommand> [OPTIONS]
```

Manage available and active plugins.

Install, remove, view, enable, and disable plugins registered with mngr.
Plugins provide agent types, provider backends, CLI commands, and lifecycle hooks.

Alias: plug

**Usage:**

```text
mngr plugin [OPTIONS] COMMAND [ARGS]...
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## mngr plugin list

List discovered plugins.

Shows all plugins registered with mngr, including built-in plugins
and any externally installed plugins.

Supports custom format templates via --format. Available fields:
name, version, description, enabled.

**Usage:**

```text
mngr plugin list [OPTIONS]
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--active` | boolean | Show only currently enabled plugins | `False` |
| `--fields` | text | Comma-separated list of fields to display (name, version, description, enabled) | None |


## Examples

**List all plugins**

```bash
$ mngr plugin list
```

**List only active plugins**

```bash
$ mngr plugin list --active
```

**Output as JSON**

```bash
$ mngr plugin list --format json
```

**Show specific fields**

```bash
$ mngr plugin list --fields name,enabled
```

**Custom format template**

```bash
$ mngr plugin list --format '{name}\t{enabled}'
```

## mngr plugin add

Install a plugin package.

All source types are repeatable and can be freely mixed in one command.
NAME is a PyPI package specifier (e.g., 'mngr-pair' or 'mngr-pair>=1.0').
--path installs from a local directory in editable mode.
--git installs from a git URL.
All plugins are installed in a single operation for speed.

**Usage:**

```text
mngr plugin add [OPTIONS] [NAMES]...
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--path` | text | Install from a local path (editable mode) [repeatable] | None |
| `--git` | text | Install from a git URL [repeatable] | None |


## Examples

**Install from PyPI**

```bash
$ mngr plugin add mngr-pair
```

**Install with version constraint**

```bash
$ mngr plugin add mngr-pair>=1.0
```

**Install from a local path**

```bash
$ mngr plugin add --path ./my-plugin
```

**Install multiple local plugins**

```bash
$ mngr plugin add --path ./plugin-a --path ./plugin-b
```

**Install from a git URL**

```bash
$ mngr plugin add --git https://github.com/user/mngr-plugin.git
```

**Mix all source types**

```bash
$ mngr plugin add pkg-a --path ./local-b --git https://example.com/c.git
```

## mngr plugin remove

Uninstall a plugin package.

Both source types are repeatable and can be freely mixed in one command.
For local paths, the package name is read from pyproject.toml.
All plugins are removed in a single operation.

**Usage:**

```text
mngr plugin remove [OPTIONS] [NAMES]...
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--path` | text | Remove by local path (reads package name from pyproject.toml) [repeatable] | None |


## Examples

**Remove by name**

```bash
$ mngr plugin remove mngr-pair
```

**Remove multiple by name**

```bash
$ mngr plugin remove mngr-pair mngr-opencode
```

**Remove by local path**

```bash
$ mngr plugin remove --path ./my-plugin
```

**Mix names and paths**

```bash
$ mngr plugin remove mngr-pair --path ./my-plugin
```

## mngr plugin enable

Enable a plugin.

Sets plugins.<name>.enabled = true in the configuration file at the
specified scope.

**Usage:**

```text
mngr plugin enable [OPTIONS] NAME
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--scope` | choice (`user` &#x7C; `project` &#x7C; `local`) | Config scope: user (~/.mngr/profiles/<profile_id>/), project (.mngr/), or local (.mngr/settings.local.toml) | `project` |


## Examples

**Enable at project scope (default)**

```bash
$ mngr plugin enable modal
```

**Enable at user scope**

```bash
$ mngr plugin enable modal --scope user
```

**Output as JSON**

```bash
$ mngr plugin enable modal --format json
```

## mngr plugin disable

Disable a plugin.

Sets plugins.<name>.enabled = false in the configuration file at the
specified scope.

**Usage:**

```text
mngr plugin disable [OPTIONS] NAME
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--scope` | choice (`user` &#x7C; `project` &#x7C; `local`) | Config scope: user (~/.mngr/profiles/<profile_id>/), project (.mngr/), or local (.mngr/settings.local.toml) | `project` |


## Examples

**Disable at project scope (default)**

```bash
$ mngr plugin disable modal
```

**Disable at user scope**

```bash
$ mngr plugin disable modal --scope user
```

**Output as JSON**

```bash
$ mngr plugin disable modal --format json
```

## mngr plugin install-wizard

Interactive wizard to install recommended plugins.

Presents a TUI with recommended plugins and lets you select which
ones to install. Plugins are installed in a single operation.

Pre-selects mngr-tutor by default. Use Space to toggle selections,
Enter to confirm, and q or Ctrl+C to cancel.

**Usage:**

```text
mngr plugin install-wizard [OPTIONS]
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |


## Examples

**Launch the plugin install wizard**

```bash
$ mngr plugin install-wizard
```

## See Also

- [mngr config](./config.md) - Manage mngr configuration

## Examples

**List all plugins**

```bash
$ mngr plugin list
```

**List only active plugins**

```bash
$ mngr plugin list --active
```

**List plugins as JSON**

```bash
$ mngr plugin list --format json
```

**Show specific fields**

```bash
$ mngr plugin list --fields name,enabled
```

**Install a plugin from PyPI**

```bash
$ mngr plugin add mngr-pair
```

**Install a local plugin**

```bash
$ mngr plugin add --path ./my-plugin
```

**Install multiple plugins at once**

```bash
$ mngr plugin add pkg-a --path ./local-b --git https://example.com/c.git
```

**Remove a plugin**

```bash
$ mngr plugin remove mngr-pair
```

**Enable a plugin**

```bash
$ mngr plugin enable modal
```

**Disable a plugin**

```bash
$ mngr plugin disable modal --scope user
```
