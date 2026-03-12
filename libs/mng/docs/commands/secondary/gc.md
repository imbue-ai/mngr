<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng gc

**Synopsis:**

```text
mng gc [OPTIONS]
```

Garbage collect unused resources.

Automatically removes containers, old snapshots, unused hosts, cached images,
and any resources that are associated with destroyed hosts and agents.

`mng destroy` automatically cleans up resources when an agent is deleted.
`mng gc` can be used to manually trigger garbage collection of unused
resources at any time.

**Usage:**

```text
mng gc [OPTIONS]
```
**Options:**

## What to Clean - Agent Resources

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--all-agent-resources` | boolean | Clean all agent resource types (machines, snapshots, volumes, work dirs) | `False` |
| `--machines` | boolean | Remove unused containers, instances, and sandboxes | `False` |
| `--snapshots` | boolean | Remove unused snapshots | `False` |
| `--volumes` | boolean | Remove unused volumes | `False` |
| `--work-dirs` | boolean | Remove work directories (git worktrees/clones) not in use by any agent | `False` |

## What to Clean - Mng Resources

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--logs` | boolean | Remove log files from destroyed agents/hosts | `False` |
| `--build-cache` | boolean | Remove build cache entries | `False` |
| `--machine-cache` | boolean | Remove machine cache entries (per-provider) [future] | `False` |

## Scope

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--all-providers` | boolean | Clean resources across all providers | `False` |
| `--provider` | text | Clean resources for a specific provider (repeatable) | None |

## Safety

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--dry-run` | boolean | Show what would be cleaned without actually cleaning | `False` |
| `--on-error` | choice (`abort` &#x7C; `continue`) | What to do when errors occur: abort (stop immediately) or continue (keep going) | `abort` |
| `-w`, `--watch` | integer | Re-run garbage collection at the specified interval (seconds) | None |

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

- [mng cleanup](./cleanup.md) - Interactive cleanup of agents and hosts
- [mng destroy](../primary/destroy.md) - Destroy agents (includes automatic GC)
- [mng list](../primary/list.md) - List agents to find unused resources

## Examples

**Preview what would be cleaned (dry run)**

```bash
$ mng gc --work-dirs --dry-run
```

**Clean all agent resources**

```bash
$ mng gc --all-agent-resources
```

**Clean machines and snapshots for Docker**

```bash
$ mng gc --machines --snapshots --provider docker
```

**Clean logs and build cache**

```bash
$ mng gc --logs --build-cache
```
