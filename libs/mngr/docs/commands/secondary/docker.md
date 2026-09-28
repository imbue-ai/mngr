<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr docker

**Synopsis:**

```text
mngr docker [resize] [OPTIONS]
```

Docker-provider-specific commands.

Commands that only make sense for hosts on the docker provider (containers
on a local or remote Docker daemon). Everything else about a docker host is
managed through the ordinary commands (create, start, stop, destroy, list).

**Usage:**

```text
mngr docker [OPTIONS] COMMAND [ARGS]...
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## mngr docker resize

Change the CPU and memory caps of a docker host.

Rewrites the CPU and/or memory caps recorded for the host (the `--cpus` and
`--memory` flags its container was created with) and applies them to the
container with `docker update`, whether it is running or stopped. A
dimension that is not passed keeps its current value.

Docker records the caps in the container's configuration, so they survive
stop/start and daemon restarts, and mngr re-applies the recorded size on every
`mngr start` and on a snapshot restore. The cgroup caps take effect on a
running container at once; what a gVisor (runsc) container reports as its
memory total in /proc/meminfo, which anything inside the container that sizes
itself from that total goes by, follows only after a restart.

Docker refuses a CPU cap above the daemon's CPU count; such a value is reported
as an error and nothing is changed.

Swap is capped at the memory cap, so a container that hits its limit is shed by
the OOM killer instead of swapping the machine to a halt.

**Usage:**

```text
mngr docker resize [OPTIONS] HOST
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

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--cpus` | integer | Whole CPUs to cap the container at (docker refuses more than the daemon's CPU count) | None |
| `--memory` | text | Memory to cap the container at, in docker's spelling (e.g. 512m, 8g); swap is capped at the same value | None |


## Examples

**Set both caps**

```bash
$ mngr docker resize my-host --cpus 4 --memory 8g
```

**Change only the memory cap**

```bash
$ mngr docker resize my-host --memory 16g
```

**Resize a gVisor host, then restart it so the container sees the new memory total**

```bash
$ mngr docker resize my-host --memory 16g && mngr stop my-agent --stop-host && mngr start my-agent
```

## See Also

- [mngr create](../primary/create.md) - Create an agent; -s --cpus / -s --memory set a docker host's size at creation
- [mngr list](../primary/list.md) - List agents; host.resource shows each host's recorded size

## Examples

**Give a docker host 4 CPUs and 8 GB of memory**

```bash
$ mngr docker resize my-host --cpus 4 --memory 8g
```
