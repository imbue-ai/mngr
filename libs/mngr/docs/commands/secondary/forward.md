<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr forward

**Synopsis:**

```text
mngr forward [--service NAME | --forward-port REMOTE_PORT] [OPTIONS]
```

Forward web traffic to agents via <agent>.localhost subdomains [experimental].

Runs a local HTTP/WS proxy that serves
``<agent-id>.localhost:<port>/*`` and byte-forwards each request to the
configured backend (a service URL discovered via ``mngr observe``/``mngr event``,
or a fixed remote port). Remote agents are reached via SSH tunnels.

Authentication uses a one-time login URL printed on stderr; in subprocess
mode the same URL is also emitted on stdout as a JSONL ``login_url`` event.
Browser sessions survive SIGHUP-driven observe restarts because the cookie
signing key is persisted to disk under ``$MNGR_HOST_DIR/plugin/forward/``.

**Usage:**

```text
mngr forward [OPTIONS]
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
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--host` | text | Bind host | `127.0.0.1` |
| `--port` | integer | Bind port | `8421` |
| `--service` | text | Service name to forward (e.g. 'system_interface') | None |
| `--forward-port` | integer | Forward to a fixed remote port on the agent's host (manual mode). Mutually exclusive with --service. | None |
| `--reverse` | text | Reverse tunnel pair REMOTE:LOCAL. Repeatable. REMOTE may be 0 (sshd-assigned). | None |
| `--no-observe` | boolean | Do not spawn `mngr observe` / `mngr event`; take a single `mngr list` snapshot instead. Requires --forward-port. | `False` |
| `--agent-include` | text | CEL expression to include agents (repeatable). Default: include every discovered agent. | None |
| `--agent-exclude` | text | CEL expression to exclude agents (repeatable). | None |
| `--event-include` | text | CEL expression to include `mngr event` source streams (repeatable). | None |
| `--event-exclude` | text | CEL expression to exclude `mngr event` source streams (repeatable). | None |
| `--preauth-cookie` | text | Pre-shared cookie value accepted in lieu of an OTP-issued cookie. | None |
| `--open-browser`, `--no-open-browser` | boolean | Open the printed login URL in the system browser. | `False` |

## Examples

**Forward system_interface for every workspace agent**

```bash
$ mngr forward --service system_interface
```

**Manual mode against a fixed port**

```bash
$ mngr forward --no-observe --forward-port 8080
```

**Set up reverse tunnels**

```bash
$ mngr forward --service system_interface --reverse 8420:8420
```

**Filter to a single label set**

```bash
$ mngr forward --service system_interface --agent-include 'has(agent.labels.workspace)'
```
