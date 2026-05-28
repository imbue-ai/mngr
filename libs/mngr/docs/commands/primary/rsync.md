<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr rsync

**Synopsis:**

```text
mngr rsync SOURCE DESTINATION [--dry-run] [--delete] [--start/--no-start] [--uncommitted-changes MODE]
```

Rsync files between a local path and a remote host or agent.

Rsync files between two endpoints, one of which must be on the local machine.

Each endpoint is a host-location address: ``AGENT[@HOST[.PROVIDER]][:PATH]``,
``@HOST[.PROVIDER]:PATH``, or a bare local path. The local side is implicit
for a bare path; ``./``, ``../``, ``/``, and ``~/`` prefixes are honored.

Exactly one of SOURCE and DESTINATION must reference a remote host or agent;
the other must be a local path. Local-to-local and remote-to-remote transfers
are not supported -- use plain ``rsync`` for the former.

**Usage:**

```text
mngr rsync [OPTIONS] SOURCE DESTINATION
```
## Arguments

- `SOURCE`: The source
- `DESTINATION`: The destination

**Options:**

## Sync Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--dry-run` | boolean | Show what would be transferred without actually transferring | `False` |
| `--start`, `--no-start` | boolean | Automatically start a host if offline (the agent does not need to be running) | `True` |
| `--delete`, `--no-delete` | boolean | Delete files in destination that don't exist in source | `False` |
| `--uncommitted-changes` | choice (`stash` &#x7C; `clobber` &#x7C; `merge` &#x7C; `fail`) | How to handle uncommitted changes on the side being modified (the destination): stash (stash and leave stashed), clobber (overwrite), merge (stash, sync, then unstash), fail (error if changes exist) | `fail` |

## File Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--exclude` | text | Patterns to exclude from sync [repeatable] [future] | None |
| `--include` | text | Include files matching glob pattern [repeatable] [future] | None |
| `--include-gitignored` | boolean | Include files that match .gitignore patterns [future] | `False` |
| `--include-file` | path | Read include patterns from file [future] | None |
| `--exclude-file` | path | Read exclude patterns from file [future] | None |

## Rsync Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--rsync-arg` | text | Additional argument to pass to rsync [repeatable] [future] | None |
| `--rsync-args` | text | Additional arguments to pass to rsync (as a single string) [future] | None |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## See Also

- [mngr git](./git.md) - Push or pull git commits between local and a remote agent or host
- [mngr pair](./pair.md) - Continuously sync files between agent and local

## Examples

**Push local files into an agent**

```bash
$ mngr rsync ./local-src my-agent
```

**Push into a subpath of an agent**

```bash
$ mngr rsync ./local-src my-agent:subdir
```

**Pull from an agent into a local directory**

```bash
$ mngr rsync my-agent ./local-copy
```

**Pull a subpath from an agent**

```bash
$ mngr rsync my-agent:src ./local-src
```

**Push to a specific host path**

```bash
$ mngr rsync ./local-src @host.modal:/work
```

**Preview what would be transferred**

```bash
$ mngr rsync ./local-src my-agent --dry-run
```
