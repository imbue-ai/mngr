<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr git

**Synopsis:**

```text
mngr git push|pull TARGET [-- GIT_ARGS...]
```

Push or pull git commits between local and a remote host or agent.

Subcommand group for git-mediated synchronization with agents and remote hosts.

Each subcommand takes a single host-location address identifying the remote
endpoint (the local side is the current working directory). The address must
include an agent or a host -- bare local paths are rejected (use plain
``git push``/``git pull`` for local-only operations).

These are thin wrappers around ``git push`` / ``git pull``. mngr builds the
SSH URL and credentials for you, then runs the underlying git command;
anything you put after ``--`` is passed through verbatim. There's no
``--source-branch``, ``--target-branch``, ``--mirror``, or ``--dry-run`` --
use the corresponding git flags (``--force``, ``--tags``, refspec syntax,
``--dry-run``, ``--rebase`` etc.) directly.

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

The local side is always the current working directory. mngr sets the
destination's ``receive.denyCurrentBranch=updateInstead`` and configures the
SSH transport, then runs ``git push <URL> <GIT_ARGS...>``. Any flags or
refspecs you supply after ``--`` are passed verbatim to the underlying
``git push``.

**Usage:**

```text
mngr git push [OPTIONS] TARGET [-- GIT_ARGS...]
```
**Options:**

## Sync Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--start`, `--no-start` | boolean | Automatically start the host if offline (the agent does not need to be running) | `True` |

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

**Push a specific branch with a refspec**

```bash
$ mngr git push my-agent -- feature:main
```

**Force-push all branches**

```bash
$ mngr git push my-agent -- --force --all
```

**Push to a path on a specific host**

```bash
$ mngr git push @host.modal:/work
```

**Preview what would be transferred**

```bash
$ mngr git push my-agent -- --dry-run
```

## mngr git pull

Pull git commits from a remote agent or host into the local repository.

Pull git commits from a remote agent or host's repository into the current
working directory's repository.

SOURCE is a host-location address: ``AGENT[@HOST[.PROVIDER]][:PATH]`` or
``@HOST[.PROVIDER]:PATH``. A bare path is rejected (use plain ``git pull``).

The local side is always the current working directory. mngr configures the
SSH transport, then runs ``git pull <URL> <GIT_ARGS...>``. Any flags or
branch names you supply after ``--`` are passed verbatim to the underlying
``git pull``.

**Usage:**

```text
mngr git pull [OPTIONS] SOURCE [-- GIT_ARGS...]
```
**Options:**

## Sync Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--start`, `--no-start` | boolean | Automatically start the host if offline (the agent does not need to be running) | `True` |

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
$ mngr git pull my-agent -- feature
```

**Rebase local changes onto agent's branch**

```bash
$ mngr git pull my-agent -- feature --rebase
```

**Pull from a path on a specific host**

```bash
$ mngr git pull @host.modal:/work
```

**Preview what would be merged**

```bash
$ mngr git pull my-agent -- --dry-run
```

## See Also

- [mngr rsync](./rsync.md) - Rsync files between local and a remote host or agent
- [mngr pair](./pair.md) - Continuously sync files between agent and local

## Examples

**Push the current branch to an agent**

```bash
$ mngr git push my-agent
```

**Push with a refspec**

```bash
$ mngr git push my-agent -- feature:main
```

**Pull the agent's branch into the current working directory**

```bash
$ mngr git pull my-agent
```

**Pull a specific branch with rebase**

```bash
$ mngr git pull my-agent -- feature --rebase
```
