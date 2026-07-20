<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr foreman

**Synopsis:**

```text
mngr foreman [--host HOST] [--port PORT] [OPTIONS]
```

Always-on web remote control for your mngr agents [experimental].

Runs a single Flask server on this box, over every agent in
mngr's view: a mobile-friendly chat UI for claude agents (live transcript with
markdown, syntax highlighting, KaTeX, mermaid, inline images and file uploads;
send messages; interrupt) and a web terminal (xterm.js over a pty bridge) for
any agent type.

No code is deployed to target boxes and there is no auth by design -- bind to a
tailnet IP or firewall the port. Create agents with plain ``mngr create``;
there is no foreman-specific create command or label filter.

**Usage:**

```text
mngr foreman [OPTIONS]
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
| `--host` | text | Bind host (default from config, else 0.0.0.0). | None |
| `--port` | integer | Bind port (default from config, else 8700). | None |

## Examples

**Serve on the default port**

```bash
$ mngr foreman --port 8700
```

**Bind to a specific tailnet IP**

```bash
$ mngr foreman --host 100.64.0.1
```
