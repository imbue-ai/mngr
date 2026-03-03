<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng pair

**Synopsis:**

```text
mng pair [SOURCE] [--target <DIR>] [--sync-direction <DIR>] [--conflict <MODE>]
```

Continuously sync files between an agent and local directory [experimental].

This command establishes a bidirectional file sync between an agent's working
directory and a local directory. Changes are watched and synced in real-time.

If git repositories exist on both sides, the command first synchronizes git
state (branches and commits) before starting the continuous file sync.

Press Ctrl+C to stop the sync.

During rapid concurrent edits, changes will be debounced to avoid partial writes [future].

**Usage:**

```text
mng pair [OPTIONS] SOURCE
```
## Arguments

- `SOURCE`: The source (optional)

**Options:**

## Source Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--source` | text | Source specification: AGENT, AGENT:PATH, or PATH | None |
| `--source-agent` | text | Source agent name or ID | None |
| `--source-host` | text | Source host name or ID | None |
| `--source-path` | text | Path within the agent's work directory | None |

## Target

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--target` | path | Local target directory [default: nearest git root or current directory] | None |

## Git Handling

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--require-git`, `--no-require-git` | boolean | Require that both source and target are git repositories [default: require git] | `True` |
| `--uncommitted-changes` | choice (`stash` &#x7C; `clobber` &#x7C; `merge` &#x7C; `fail`) | How to handle uncommitted changes during initial git sync. The initial sync aborts immediately if unresolved conflicts exist, regardless of this setting. | `fail` |

## Sync Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--sync-direction` | choice (`both` &#x7C; `forward` &#x7C; `reverse`) | Sync direction: both (bidirectional), forward (source->target), reverse (target->source) | `both` |
| `--conflict` | choice (`newer` &#x7C; `source` &#x7C; `target` &#x7C; `ask`) | Conflict resolution mode (only matters for bidirectional sync). 'newer' prefers the file with the more recent modification time (uses unison's -prefer newer; note that clock skew between machines can cause incorrect results). 'source' and 'target' always prefer that side. 'ask' prompts interactively [future]. | `newer` |

## File Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--include` | text | Include files matching glob pattern [repeatable] | None |
| `--exclude` | text | Exclude files matching glob pattern [repeatable] | None |

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

## See Also

- [mng push](./push.md) - Push files or git commits to an agent
- [mng pull](./pull.md) - Pull files or git commits from an agent
- [mng create](./create.md) - Create a new agent
- [mng list](./list.md) - List agents to find one to pair with

## Examples

**Pair with an agent**

```bash
$ mng pair my-agent
```

**Pair to specific local directory**

```bash
$ mng pair my-agent --target ./local-dir
```

**One-way sync (source to target)**

```bash
$ mng pair my-agent --sync-direction=forward
```

**Prefer source on conflicts**

```bash
$ mng pair my-agent --conflict=source
```

**Filter to specific host**

```bash
$ mng pair my-agent --source-host @local
```

**Use --source-agent flag**

```bash
$ mng pair --source-agent my-agent --target ./local-copy
```
