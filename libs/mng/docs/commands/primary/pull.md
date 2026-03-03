<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng pull

**Synopsis:**

```text
mng pull [SOURCE] [DESTINATION] [--source-agent <AGENT>] [--dry-run] [--stop]
```

Pull files or git commits from an agent to local machine [experimental].

Syncs files or git state from an agent's working directory to a local directory.
Default behavior uses rsync for efficient incremental file transfer.
Use --sync-mode=git to merge git branches instead of syncing files.

If no source is specified, shows an interactive selector to choose an agent.

**Usage:**

```text
mng pull [OPTIONS] SOURCE DESTINATION
```
## Arguments

- `SOURCE`: The source (optional)
- `DESTINATION`: The destination (optional)

**Options:**

## Source Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--source` | text | Source specification: AGENT, AGENT:PATH, or PATH | None |
| `--source-agent` | text | Source agent name or ID | None |
| `--source-host` | text | Source host name or ID [future] | None |
| `--source-path` | text | Path within the agent's work directory | None |

## Destination

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--destination` | path | Local destination directory [default: .] | None |

## Sync Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--dry-run` | boolean | Show what would be transferred without actually transferring | `False` |
| `--stop` | boolean | Stop the agent after pulling (for state consistency) | `False` |
| `--delete`, `--no-delete` | boolean | Delete files in destination that don't exist in source | `False` |
| `--sync-mode` | choice (`files` &#x7C; `git` &#x7C; `full`) | What to sync: files (working directory via rsync), git (merge git branches), or full (everything) [future] | `files` |
| `--exclude` | text | Patterns to exclude from sync [repeatable] [future] | None |

## Target (for agent-to-agent sync)

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--target` | text | Target specification: AGENT, AGENT.HOST, AGENT.HOST:PATH, or HOST:PATH [future] | None |
| `--target-agent` | text | Target agent name or ID [future] | None |
| `--target-host` | text | Target host name or ID [future] | None |
| `--target-path` | text | Path within target to sync to [future] | None |

## Multi-source

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--stdin` | boolean | Read source agents/hosts from stdin, one per line [future] | `False` |

## File Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--include` | text | Include files matching glob pattern [repeatable] [future] | None |
| `--include-gitignored` | boolean | Include files that match .gitignore patterns [future] | `False` |
| `--include-file` | path | Read include patterns from file [future] | None |
| `--exclude-file` | path | Read exclude patterns from file [future] | None |

## Rsync Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--rsync-arg` | text | Additional argument to pass to rsync [repeatable] [future] | None |
| `--rsync-args` | text | Additional arguments to pass to rsync (as a single string) [future] | None |

## Git Sync Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--branch` | text | Pull a specific branch [repeatable] [future] | None |
| `--target-branch` | text | Branch to merge into (git mode only) [default: current branch] | None |
| `--all-branches`, `--all` | boolean | Pull all remote branches [future] | `False` |
| `--tags` | boolean | Include git tags in sync [future] | `False` |
| `--force-git` | boolean | Force overwrite local git state (use with caution) [future]. Without this flag, the command fails if local and remote history have diverged (e.g. after a force-push) and the user must resolve manually. | `False` |
| `--merge` | boolean | Merge remote changes with local changes [future] | `False` |
| `--rebase` | boolean | Rebase local changes onto remote changes [future] | `False` |
| `--uncommitted-source` | choice (`warn` &#x7C; `error`) | Warn or error if source has uncommitted changes [future] | None |
| `--uncommitted-changes` | choice (`stash` &#x7C; `clobber` &#x7C; `merge` &#x7C; `fail`) | How to handle uncommitted changes in the destination: stash (stash and leave stashed), clobber (overwrite), merge (stash, pull, unstash), fail (error if changes exist) | `fail` |

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

## Multi-target Behavior

See [multi_target](../generic/multi_target.md) for options controlling behavior when some agents cannot be processed.

## See Also

- [mng create](./create.md) - Create a new agent
- [mng list](./list.md) - List agents to find one to pull from
- [mng connect](./connect.md) - Connect to an agent interactively
- [mng push](./push.md) - Push files or git commits to an agent

## Examples

**Pull from agent to current directory**

```bash
$ mng pull my-agent
```

**Pull to specific local directory**

```bash
$ mng pull my-agent ./local-copy
```

**Pull specific subdirectory**

```bash
$ mng pull my-agent:src ./local-src
```

**Preview what would be transferred**

```bash
$ mng pull my-agent --dry-run
```

**Pull git commits**

```bash
$ mng pull my-agent --sync-mode=git
```
