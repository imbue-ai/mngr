<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr git

**Synopsis:**

```text
mngr git push|pull TARGET [OPTIONS]
```

Push or pull git commits between local and a remote host or agent.

Subcommand group for git-mediated synchronization with agents and remote hosts.

Each subcommand takes a single host-location address identifying the remote
endpoint (the local side is the current working directory). The address must
include an agent or a host -- bare local paths are rejected (use plain
``git push``/``git pull`` for local-only operations).

**Usage:**

```text
mngr git [OPTIONS] COMMAND [ARGS]...
```
**Options:**

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

## mngr git push

Push git commits from the local repository to a remote agent or host.

Push git commits from the current working directory's repository to a remote
agent or host's repository.

TARGET is a host-location address: ``AGENT[@HOST[.PROVIDER]][:PATH]`` or
``@HOST[.PROVIDER]:PATH``. A bare path is rejected (use plain ``git push``).

The local side is always the current working directory.

**Usage:**

```text
mngr git push [OPTIONS] TARGET
```
**Options:**

## Sync Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--dry-run` | boolean | Show what would be transferred without actually transferring | `False` |
| `--start`, `--no-start` | boolean | Automatically start the host if offline (the agent does not need to be running) | `True` |

## Git Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--source-branch` | text | Branch to push from [default: current branch on the local machine] | None |
| `--target-branch` | text | Branch to push to [default: current branch on the remote] | None |
| `--mirror` | boolean | Force the remote's git state to match the source, overwriting all refs (branches, tags) and resetting the working tree (dangerous). Any commits or branches that exist only on the remote will be lost. Required when the remote and the source have diverged (non-fast-forward). For non-local hosts, pushes all local branches and tags. | `False` |
| `--uncommitted-changes` | choice (`stash` &#x7C; `clobber` &#x7C; `merge` &#x7C; `fail`) | How to handle uncommitted changes on the remote (the side being modified): stash, clobber, merge, or fail | `fail` |
| `--branch` | text | Push specific branches [repeatable] [future] | None |
| `--all-branches`, `--all` | boolean | Push all branches [future] | `False` |
| `--tags` | boolean | Include git tags in push [future] | `False` |

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


## Examples

**Push the current branch to an agent**

```bash
$ mngr git push my-agent
```

**Push a specific branch**

```bash
$ mngr git push my-agent --source-branch feature
```

**Force-overwrite the agent's refs**

```bash
$ mngr git push my-agent --mirror
```

**Push to a path on a specific host**

```bash
$ mngr git push @host.modal:/work
```

**Preview what would be transferred**

```bash
$ mngr git push my-agent --dry-run
```

## mngr git pull

Pull git commits from a remote agent or host into the local repository.

Pull git commits from a remote agent or host's repository into the current
working directory's repository (by fetching and merging).

SOURCE is a host-location address: ``AGENT[@HOST[.PROVIDER]][:PATH]`` or
``@HOST[.PROVIDER]:PATH``. A bare path is rejected (use plain ``git pull``).

The local side is always the current working directory.

**Usage:**

```text
mngr git pull [OPTIONS] SOURCE
```
**Options:**

## Sync Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--dry-run` | boolean | Show what would be transferred without actually transferring | `False` |
| `--start`, `--no-start` | boolean | Automatically start the host if offline (the agent does not need to be running) | `True` |

## Git Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--source-branch` | text | Branch to pull from [default: current branch on the remote] | None |
| `--target-branch` | text | Branch to merge into on the local side [default: current branch locally] | None |
| `--uncommitted-changes` | choice (`stash` &#x7C; `clobber` &#x7C; `merge` &#x7C; `fail`) | How to handle uncommitted changes locally (the side being modified): stash, clobber, merge, or fail | `fail` |
| `--branch` | text | Pull specific branches [repeatable] [future] | None |
| `--all-branches`, `--all` | boolean | Pull all remote branches [future] | `False` |
| `--tags` | boolean | Include git tags in sync [future] | `False` |
| `--force-git` | boolean | Force overwrite local git state (use with caution) [future] | `False` |
| `--merge` | boolean | Merge remote changes with local changes [future] | `False` |
| `--rebase` | boolean | Rebase local changes onto remote changes [future] | `False` |
| `--uncommitted-source` | choice (`warn` &#x7C; `error`) | Warn or error if the remote has uncommitted changes [future] | None |

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


## Examples

**Pull the current branch from an agent**

```bash
$ mngr git pull my-agent
```

**Pull a specific branch**

```bash
$ mngr git pull my-agent --source-branch feature
```

**Pull from a path on a specific host**

```bash
$ mngr git pull @host.modal:/work
```

**Preview what would be merged**

```bash
$ mngr git pull my-agent --dry-run
```

## See Also

- [mngr rsync](./rsync.md) - Rsync files between local and a remote host or agent
- [mngr pair](./pair.md) - Continuously sync files between agent and local

## Examples

**Push the current branch to an agent**

```bash
$ mngr git push my-agent
```

**Pull the agent's branch into the current working directory**

```bash
$ mngr git pull my-agent
```
