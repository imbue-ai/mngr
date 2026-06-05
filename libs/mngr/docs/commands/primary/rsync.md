<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr rsync

**Synopsis:**

```text
mngr rsync SOURCE DESTINATION [--start/--no-start] [--uncommitted-changes MODE] [-- RSYNC_ARGS...]
```

Rsync files between a local path and a remote host or agent.

Rsync files between two endpoints, one of which must be on the local machine.

Each endpoint is a host-location address: ``AGENT[@HOST[.PROVIDER]][:PATH]``,
``@HOST[.PROVIDER]:PATH``, or a bare local path. The local side is implicit
for a bare path; ``./``, ``../``, ``/``, and ``~/`` prefixes are honored.

**Agent paths**: a ``:PATH`` on an agent endpoint is taken relative to that
agent's workdir unless it is absolute. ``my-agent:runtime/reports`` therefore
means ``runtime/reports`` *inside the agent's worktree*, regardless of where
you run the command; pass an absolute ``:PATH`` to target an exact location.

mngr is a thin wrapper around ``rsync``. Anything you pass after ``--`` is
forwarded verbatim (use ``--dry-run``, ``--delete``, ``--exclude=PATTERN``,
``--include-from=FILE``, etc. directly).

**Trailing slashes**: rsync interprets a trailing ``/`` on the source as "copy
contents into destination" rather than "copy source itself as a child of
destination". mngr passes your paths through unchanged, so you control the
slash. The one exception: when you reference an agent or host *by name only*
(no ``:PATH``), the resolved workdir is suffixed with ``/`` automatically --
that's almost always what you want.

Exactly one of SOURCE and DESTINATION must reference a remote host or agent;
the other must be a local path. Local-to-local and remote-to-remote transfers
are rejected -- use plain ``rsync`` for local-to-local.

**Usage:**

```text
mngr rsync [OPTIONS] SOURCE DESTINATION [-- RSYNC_ARGS...]
```
## Arguments

- `SOURCE`: The source
- `DESTINATION`: The destination
- `[-- RSYNC_ARGS...]`: Additional arguments passed through

**Options:**

## Sync Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--start`, `--no-start` | boolean | Automatically start a host if offline (the agent does not need to be running) | `True` |
| `--uncommitted-changes` | choice (`stash` &#x7C; `clobber` &#x7C; `merge` &#x7C; `fail`) | How to handle uncommitted changes on the side being modified (the destination): stash (stash and leave stashed), clobber (overwrite), merge (stash, sync, then unstash), fail (error if changes exist) | `fail` |

## File Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--include-gitignored` | boolean | Include files that match .gitignore patterns [future] | `False` |

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

**Push contents of an agent's workdir into a local dir**

```bash
$ mngr rsync my-agent ./local-copy
```

**Push local files into an agent's workdir**

```bash
$ mngr rsync ./local-src/ my-agent
```

**Push local dir as a child of agent's workdir (no source slash)**

```bash
$ mngr rsync ./local-src my-agent
```

**Push into a subpath of an agent**

```bash
$ mngr rsync ./local-src/ my-agent:subdir
```

**Push to a specific host path**

```bash
$ mngr rsync ./local-src/ @host.modal:/work
```

**Preview what would be transferred**

```bash
$ mngr rsync ./local-src my-agent -- --dry-run
```

**Delete files in destination that aren't in source**

```bash
$ mngr rsync ./local-src/ my-agent -- --delete
```
