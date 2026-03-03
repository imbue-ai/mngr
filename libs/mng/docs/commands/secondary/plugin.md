<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng plugin

**Synopsis:**

```text
mng [plugin|plug] <subcommand> [OPTIONS]
```

Manage available and active plugins [experimental].

Install, remove, view, enable, and disable plugins registered with mng.
Plugins provide agent types, provider backends, CLI commands, and lifecycle hooks.

Alias: plug

**Usage:**

```text
mng plugin [OPTIONS] COMMAND [ARGS]...
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided [experimental], fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `--json` | boolean | Alias for --format json | `False` |
| `--jsonl` | boolean | Alias for --format jsonl | `False` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## mng plugin list

List discovered plugins [experimental].

Shows all plugins registered with mng, including built-in plugins
and any externally installed plugins.

Supports custom format templates via --format. Available fields:
name, version, description, enabled.

**Usage:**

```text
mng plugin list [OPTIONS]
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided [experimental], fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `--json` | boolean | Alias for --format json | `False` |
| `--jsonl` | boolean | Alias for --format jsonl | `False` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
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
$ mng plugin list
```

**List only active plugins**

```bash
$ mng plugin list --active
```

**Output as JSON**

```bash
$ mng plugin list --format json
```

**Show specific fields**

```bash
$ mng plugin list --fields name,enabled
```

**Custom format template**

```bash
$ mng plugin list --format '{name}\t{enabled}'
```

## mng plugin add

Install a plugin package [experimental].

Provide exactly one of NAME (positional), --path, or --git. NAME is a PyPI
package specifier (e.g., 'mng-pair' or 'mng-pair>=1.0'). --path installs
from a local directory in editable mode. --git installs from a git URL.

**Usage:**

```text
mng plugin add [OPTIONS] [NAME]
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided [experimental], fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `--json` | boolean | Alias for --format json | `False` |
| `--jsonl` | boolean | Alias for --format jsonl | `False` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--path` | text | Install from a local path (editable mode) | None |
| `--git` | text | Install from a git URL | None |


## Examples

**Install from PyPI**

```bash
$ mng plugin add mng-pair
```

**Install with version constraint**

```bash
$ mng plugin add mng-pair>=1.0
```

**Install from a local path**

```bash
$ mng plugin add --path ./my-plugin
```

**Install from a git URL**

```bash
$ mng plugin add --git https://github.com/user/mng-plugin.git
```

## mng plugin remove

Uninstall a plugin package [experimental].

Provide exactly one of NAME (positional) or --path. For local paths,
the package name is read from pyproject.toml.

**Usage:**

```text
mng plugin remove [OPTIONS] [NAME]
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided [experimental], fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `--json` | boolean | Alias for --format json | `False` |
| `--jsonl` | boolean | Alias for --format jsonl | `False` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--path` | text | Remove by local path (reads package name from pyproject.toml) | None |


## Examples

**Remove by name**

```bash
$ mng plugin remove mng-pair
```

**Remove by local path**

```bash
$ mng plugin remove --path ./my-plugin
```

## mng plugin enable

Enable a plugin [experimental].

Sets plugins.<name>.enabled = true in the configuration file at the
specified scope.

**Usage:**

```text
mng plugin enable [OPTIONS] NAME
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided [experimental], fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `--json` | boolean | Alias for --format json | `False` |
| `--jsonl` | boolean | Alias for --format jsonl | `False` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--scope` | choice (`user` &#x7C; `project` &#x7C; `local`) | Config scope: user (~/.mng/profiles/<profile_id>/), project (.mng/), or local (.mng/settings.local.toml) | `project` |


## Examples

**Enable at project scope (default)**

```bash
$ mng plugin enable modal
```

**Enable at user scope**

```bash
$ mng plugin enable modal --scope user
```

**Output as JSON**

```bash
$ mng plugin enable modal --format json
```

## mng plugin disable

Disable a plugin [experimental].

Sets plugins.<name>.enabled = false in the configuration file at the
specified scope.

**Usage:**

```text
mng plugin disable [OPTIONS] NAME
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided [experimental], fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `--json` | boolean | Alias for --format json | `False` |
| `--jsonl` | boolean | Alias for --format jsonl | `False` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--scope` | choice (`user` &#x7C; `project` &#x7C; `local`) | Config scope: user (~/.mng/profiles/<profile_id>/), project (.mng/), or local (.mng/settings.local.toml) | `project` |


## Examples

**Disable at project scope (default)**

```bash
$ mng plugin disable modal
```

**Disable at user scope**

```bash
$ mng plugin disable modal --scope user
```

**Output as JSON**

```bash
$ mng plugin disable modal --format json
```

## See Also

- [mng config](./config.md) - Manage mng configuration

## Examples

**List all plugins**

```bash
$ mng plugin list
```

**List only active plugins**

```bash
$ mng plugin list --active
```

**List plugins as JSON**

```bash
$ mng plugin list --format json
```

**Show specific fields**

```bash
$ mng plugin list --fields name,enabled
```

**Install a plugin from PyPI**

```bash
$ mng plugin add mng-pair
```

**Install a local plugin**

```bash
$ mng plugin add --path ./my-plugin
```

**Install a plugin from git**

```bash
$ mng plugin add --git https://github.com/user/mng-plugin.git
```

**Remove a plugin**

```bash
$ mng plugin remove mng-pair
```

**Enable a plugin**

```bash
$ mng plugin enable modal
```

**Disable a plugin**

```bash
$ mng plugin disable modal --scope user
```
