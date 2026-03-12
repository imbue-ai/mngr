<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng provision

**Synopsis:**

```text
mng [provision|prov] [AGENT] [--agent <AGENT>] [--user-command <CMD>] [--upload-file <LOCAL:REMOTE>] [--env <KEY=VALUE>]
```

Re-run provisioning on an existing agent [experimental].

This re-runs the provisioning steps (plugin lifecycle hooks, file transfers,
user commands, env vars) on an agent that has already been created. Useful for
syncing configuration, authentication, and installing additional packages. Most
provisioning steps are specified via plugins, but custom steps can also be
defined using the options below.

The agent's existing environment variables are preserved. New env vars from
--env, --env-file, and --pass-env override existing ones with the same key.

By default, if the agent is running, it is stopped before provisioning and
restarted after. This ensures config and env var changes take effect. Use
--no-restart to skip the restart for non-disruptive changes like installing
packages.

Provisioning is done per agent, but changes are visible to other agents on the
same host. Be careful to avoid conflicts when provisioning multiple agents on
the same host.

Alias: prov

**Usage:**

```text
mng provision [OPTIONS] [AGENT]
```
## Arguments

- `AGENT`: Agent name or ID to provision

**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | Agent name or ID to provision (alternative to positional argument) | None |
| `--host` | text | Filter by host name or ID [future] | None |

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--bootstrap` | choice (`yes` &#x7C; `warn` &#x7C; `no`) | Auto-install missing required tools: yes, warn (install with warning), or no [default: warn on remote, no on local] [future] | None |
| `--destroy-on-fail`, `--no-destroy-on-fail` | boolean | Destroy the host if provisioning fails [future] | `False` |
| `--restart`, `--no-restart` | boolean | Restart agent after provisioning (default: restart). Use --no-restart for non-disruptive changes like installing packages | `True` |

## Agent Provisioning

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--user-command` | text | Run custom shell command during provisioning [repeatable] | None |
| `--sudo-command` | text | Run custom shell command as root during provisioning [repeatable] | None |
| `--upload-file` | text | Upload LOCAL:REMOTE file pair [repeatable] | None |
| `--append-to-file` | text | Append REMOTE:TEXT to file [repeatable] | None |
| `--prepend-to-file` | text | Prepend REMOTE:TEXT to file [repeatable] | None |
| `--create-directory` | text | Create directory on remote [repeatable] | None |

## Agent Environment Variables

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--env` | text | Set environment variable KEY=VALUE | None |
| `--env-file` | path | Load env file | None |
| `--pass-env` | text | Forward variable from shell | None |

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
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## See Also

- [mng create](../primary/create.md) - Create and run an agent
- [mng connect](../primary/connect.md) - Connect to an agent
- [mng list](../primary/list.md) - List existing agents

## Examples

**Re-provision an agent**

```bash
$ mng provision my-agent
```

**Install a package without restarting**

```bash
$ mng provision my-agent --user-command 'pip install pandas' --no-restart
```

**Upload a config file**

```bash
$ mng provision my-agent --upload-file ./config.json:/app/config.json
```

**Set an environment variable**

```bash
$ mng provision my-agent --env 'API_KEY=secret'
```

**Run a root command**

```bash
$ mng provision my-agent --sudo-command 'apt-get install -y ffmpeg'
```
