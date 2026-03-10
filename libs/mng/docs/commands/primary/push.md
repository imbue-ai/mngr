<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng push

**Synopsis:**

```text
mng push [TARGET] [SOURCE] [--target-agent <AGENT>] [--dry-run] [--stop]
```

Push files or git commits from local machine to an agent [experimental].

Syncs files or git state from a local directory to an agent's working directory.
Default behavior uses rsync for efficient incremental file transfer.
Use --sync-mode=git to push git branches instead of syncing files.

If no target is specified, shows an interactive selector to choose an agent.

IMPORTANT: The source (host) workspace is never modified. Only the target
(agent workspace) may be modified.

**Usage:**

```text
mng push [OPTIONS] TARGET SOURCE
```
## Arguments

- `TARGET`: The target (optional)
- `SOURCE`: The source (optional)

**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--target` | text | Target specification: AGENT, AGENT:PATH, or PATH | None |
| `--target-agent` | text | Target agent name or ID | None |
| `--target-host` | text | Target host name or ID [future] | None |
| `--target-path` | text | Path within the agent's work directory | None |

## Source

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--source` | path | Local source directory [default: .] | None |

## Sync Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--dry-run` | boolean | Show what would be transferred without actually transferring | `False` |
| `--stop` | boolean | Stop the agent after pushing (for state consistency) | `False` |
| `--delete`, `--no-delete` | boolean | Delete files in destination that don't exist in source | `False` |
| `--sync-mode` | choice (`files` &#x7C; `git` &#x7C; `full`) | What to sync: files (working directory via rsync), git (push git branches), or full (everything) [future] | `files` |
| `--exclude` | text | Patterns to exclude from sync [repeatable] [future] | None |
| `--source-branch` | text | Branch to push from (git mode only) [default: current branch] | None |
| `--uncommitted-changes` | choice (`stash` &#x7C; `clobber` &#x7C; `merge` &#x7C; `fail`) | How to handle uncommitted changes in the agent workspace: stash (stash and leave stashed), clobber (overwrite), merge (stash, push, unstash), fail (error if changes exist) | `fail` |

## Git Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--mirror` | boolean | Force the agent's git state to match the source, overwriting all refs (branches, tags) and resetting the working tree (dangerous). Any commits or branches that exist only in the agent will be lost. Only applies to --sync-mode=git. Required when the agent and source have diverged (non-fast-forward). For remote agents, uses git push --mirror [future]. | `False` |
| `--rsync-only` | boolean | Use rsync even if git is available in both source and destination | `False` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided [experimental], fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
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

- [mng create](./create.md) - Create a new agent
- [mng list](./list.md) - List agents to find one to push to
- [mng pull](./pull.md) - Pull files or git commits from an agent
- [mng pair](./pair.md) - Continuously sync files between agent and local

## Examples

**Push to agent from current directory**

```bash
$ mng push my-agent
```

**Push from specific local directory**

```bash
$ mng push my-agent ./local-dir
```

**Push to specific subdirectory**

```bash
$ mng push my-agent:subdir ./local-src
```

**Preview what would be transferred**

```bash
$ mng push my-agent --dry-run
```

**Push git commits**

```bash
$ mng push my-agent --sync-mode=git
```

**Mirror all refs to agent**

```bash
$ mng push my-agent --sync-mode=git --mirror
```
