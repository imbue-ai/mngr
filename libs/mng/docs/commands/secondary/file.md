<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng file

**Synopsis:**

```text
mng file (get|put|list) TARGET PATH [OPTIONS]
```

Read, write, and list files on agents and hosts.

Transfer files to and from agents and hosts.

Use 'get' to read a file, 'put' to write a file, and 'list' to list
files in a directory. TARGET can be an agent or host name/ID.

Paths can be absolute or relative. For agent targets, relative paths
are resolved against the agent's work directory by default. Use
--relative-to to change the base: 'state' for the agent state
directory, or 'host' for the host directory. For host targets,
relative paths always resolve against the host directory.

**Usage:**

```text
mng file [OPTIONS] COMMAND [ARGS]...
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## mng file get

**Usage:**

```text
mng file get [OPTIONS] TARGET PATH
```
**Options:**

## Output

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--output`, `-o` | path | Write to a local file instead of stdout | None |

## Path Resolution

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--relative-to` | choice (`work` &#x7C; `state` &#x7C; `host`) | Base directory for relative paths (agent targets only): work (work_dir), state (agent state dir), host (host dir) | `work` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNG_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |

## mng file list

**Usage:**

```text
mng file list [OPTIONS] TARGET [PATH]
```
**Options:**

## Path Resolution

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--relative-to` | choice (`work` &#x7C; `state` &#x7C; `host`) | Base directory for relative paths (agent targets only): work (work_dir), state (agent state dir), host (host dir) | `work` |

## Output Format

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--fields` | text | Comma-separated list of fields to display: name, path, file_type, size, modified, permissions | None |

## Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--recursive`, `-R` | boolean | List files recursively | `False` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNG_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |

## mng file put

**Usage:**

```text
mng file put [OPTIONS] TARGET PATH
```
**Options:**

## Input

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--input`, `-i` | path | Read from a local file instead of stdin | None |

## Path Resolution

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--relative-to` | choice (`work` &#x7C; `state` &#x7C; `host`) | Base directory for relative paths (agent targets only): work (work_dir), state (agent state dir), host (host dir) | `work` |

## File Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--mode` | text | Set file permissions (e.g. '0644') | None |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNG_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |

## See Also

- [mng exec](../primary/exec.md) - Execute a shell command on an agent's host
- [mng push](../primary/push.md) - Push files or git commits to an agent
- [mng pull](../primary/pull.md) - Pull files or git commits from an agent
- [mng events](./events.md) - View agent and host event files

## Examples

**Read a file from an agent**

```bash
$ mng file get my-agent config.toml
```

**Write a file to an agent**

```bash
$ mng file put my-agent config.toml --input local.toml
```

**List files in an agent's work directory**

```bash
$ mng file list my-agent
```

**List files relative to agent state directory**

```bash
$ mng file list my-agent --relative-to state
```

**Read a file using absolute path**

```bash
$ mng file get my-agent /etc/hostname
```

**Write stdin to a file on a host**

```bash
$ echo 'hello' | mng file put my-host greeting.txt
```
