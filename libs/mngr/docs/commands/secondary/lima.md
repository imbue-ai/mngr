<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr lima

**Synopsis:**

```text
mngr lima [resize] [OPTIONS]
```

Lima-provider-specific commands.

Commands that only make sense for hosts on the lima provider (local Lima VMs).
Everything else about a lima host is managed through the ordinary commands
(create, start, stop, destroy, list).

**Usage:**

```text
mngr lima [OPTIONS] COMMAND [ARGS]...
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## mngr lima resize

Change the CPUs, memory, or data-disk size of a lima host.

Rewrites the size recorded for the host and applies it to its VM. A dimension
that is not passed keeps its current value.

The CPU count and memory are the VM's `--cpus` and `--memory`; they are written
into the instance's lima config. `--disk` is the btrfs data disk that backs the
host's data (not the VM's boot disk), grown with `limactl disk resize`; the
filesystem grows to fill it on the VM's next boot. A disk never shrinks, and a
host created with the exposed bind-mount layout has no data disk to resize.

A stopped VM is reconfigured at once. A running VM cannot be edited, so the
new size is recorded and applied when the host is next started; `mngr start`
always brings the VM up to the recorded size first. A value lima refuses is
reported as an error and changes nothing.

**Usage:**

```text
mngr lima resize [OPTIONS] HOST
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
| `--cpus` | integer | Whole CPUs to give the VM | None |
| `--memory` | float | RAM to give the VM, in GiB (e.g. 8 or 1.5) | None |
| `--disk` | integer | Size to grow the btrfs data disk to, in GiB; a value below the current size is refused | None |


## Examples

**Set all three dimensions**

```bash
$ mngr lima resize my-host --cpus 4 --memory 8 --disk 200
```

**Grow only the data disk**

```bash
$ mngr lima resize my-host --disk 300
```

**Resize a running host, then restart it so the VM boots at the new size**

```bash
$ mngr lima resize my-host --memory 16 && mngr stop my-agent --stop-host && mngr start my-agent
```

## See Also

- [mngr create](../primary/create.md) - Create an agent; -s --cpus / -s --memory / -s --disk set a lima host's size at creation
- [mngr list](../primary/list.md) - List agents; host.resource shows each host's recorded size

## Examples

**Give a lima host 4 CPUs, 8 GiB of memory, and a 200 GiB data disk**

```bash
$ mngr lima resize my-host --cpus 4 --memory 8 --disk 200
```
