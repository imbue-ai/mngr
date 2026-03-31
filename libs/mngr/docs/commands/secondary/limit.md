<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr limit

**Synopsis:**

```text
mngr [limit|lim] [AGENTS...|-] [--agent <AGENT>] [--host <HOST>] [--idle-timeout <DURATION>] [--idle-mode <MODE>] [--grant <PERM>] [--revoke <PERM>]
```

Configure limits for agents and hosts [experimental].

Agents effectively have permissions that are equivalent to the *union* of all
permissions on the same host. Changing permissions for agents requires them
to be restarted.

Changes to some limits for hosts (e.g. CPU, RAM, disk space, network) are
handled by the provider.

When targeting agents, host-level settings (idle-timeout, idle-mode,
activity-sources) are applied to each agent's underlying host.

Agent-level settings (start-on-boot, grant, revoke) require agent targeting
and cannot be used with --host alone.

Use '-' in place of agent names to read them from stdin, one per line.

Alias: lim

**Usage:**

```text
mngr limit [OPTIONS] [AGENTS]...
```
## Arguments

- `AGENTS`: Agent name(s) or ID(s) to configure (can also be specified via `--agent`)

**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | Agent name or ID to configure (can be specified multiple times) | None |
| `--host` | text | Host name or ID to configure (can be specified multiple times) | None |

## Lifecycle

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--start-on-boot`, `--no-start-on-boot` | boolean | Automatically restart agent when host restarts | None |
| `--idle-timeout` | text | Shutdown after idle for specified duration (e.g., 30s, 5m, 1h, or plain seconds) | None |
| `--idle-mode` | choice (`io` &#x7C; `user` &#x7C; `agent` &#x7C; `ssh` &#x7C; `create` &#x7C; `boot` &#x7C; `start` &#x7C; `run` &#x7C; `disabled`) | When to consider host idle | None |
| `--activity-sources` | text | Set activity sources for idle detection (comma-separated) | None |
| `--add-activity-source` | choice (`create` &#x7C; `boot` &#x7C; `start` &#x7C; `ssh` &#x7C; `process` &#x7C; `agent` &#x7C; `user`) | Add an activity source for idle detection (repeatable) | None |
| `--remove-activity-source` | choice (`create` &#x7C; `boot` &#x7C; `start` &#x7C; `ssh` &#x7C; `process` &#x7C; `agent` &#x7C; `user`) | Remove an activity source from idle detection (repeatable) | None |

## Permissions

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--grant` | text | Grant a permission to the agent (repeatable) | None |
| `--revoke` | text | Revoke a permission from the agent (repeatable) | None |

## SSH Keys

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--refresh-ssh-keys` | boolean | Refresh the SSH keys for the host [future] | `False` |
| `--add-ssh-key` | text | Add an SSH public key to the host for access (repeatable) [future] | None |
| `--remove-ssh-key` | text | Remove an SSH public key from the host (repeatable) [future] | None |

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
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Idle Modes

See [Idle Detection](../../concepts/idle_detection.md) for details on idle modes and activity sources.

## See Also

- [mngr create](../primary/create.md) - Create a new agent
- [mngr list](../primary/list.md) - List existing agents
- [mngr stop](../primary/stop.md) - Stop running agents

## Examples

**Set idle timeout for an agent's host**

```bash
$ mngr limit my-agent --idle-timeout 5m
```

**Grant permissions to an agent**

```bash
$ mngr limit my-agent --grant network --grant internet
```

**Disable idle detection for all agents**

```bash
$ mngr list --ids | mngr limit - --idle-mode disabled
```

**Update host idle settings directly**

```bash
$ mngr limit --host my-host --idle-timeout 1h
```
