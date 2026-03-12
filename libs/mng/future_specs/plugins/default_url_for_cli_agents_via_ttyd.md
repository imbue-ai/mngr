# Default URL for CLI Agents via ttyd Spec [future]

This plugin provides web terminal access to CLI-based agents by running ttyd with a security wrapper.

See [user-facing documentation](../../docs/core_plugins/default_url_for_cli_agents_via_ttyd.md) for usage.

## Problem

ttyd by default accepts connections from any origin. A malicious webpage could send requests to `localhost:<ttyd-port>` and interact with the terminal. 
We need a way to authenticate requests before allowing tmux attachment.

## Solution

Instead of running ttyd directly with `tmux attach`, we use a wrapper script that validates a secret token from the URL before attaching.

## ttyd Invocation

```bash
ttyd -W -a /usr/local/bin/mng-ttyd-wrapper
```

Flags:
- `-W` - Enable WebSocket write support (required for interactive terminals)
- `-a` - Allow URL arguments to be passed as command-line arguments to the wrapped command

When a browser connects to `http://<host>/?arg=<token>`, ttyd runs:
```bash
/usr/local/bin/mng-ttyd-wrapper <token>
```

## Wrapper Script

Location: `/usr/local/bin/mng-ttyd-wrapper`

```bash
#!/usr/bin/env bash
set -euo pipefail

EXPECTED_TOKEN=$(cat "$MNG_AGENT_STATE_DIR/plugin/default_url_for_cli_agents_via_ttyd/token")
PROVIDED_TOKEN="${1:-}"

if [[ "$PROVIDED_TOKEN" != "$EXPECTED_TOKEN" ]]; then
    echo "Invalid or missing token."
    echo "Use 'mng open <agent>' to get a valid URL."
    sleep 3
    exit 1
fi

exec tmux attach -t "mng-<agent_name>"
```

The script:
1. Reads expected token from agent's plugin state directory
2. Compares against the first argument (provided via ttyd's `-a` flag)
3. If match: attaches to the agent's tmux session
4. If mismatch: shows error, waits 3 seconds (rate limiting), exits

## Token Management

### Generation

On agent creation (`on_after_agent_create` hook):
```python
token = secrets.token_urlsafe(32)
token_path = agent_dir / "plugin" / "default_url_for_cli_agents_via_ttyd" / "token"
token_path.parent.mkdir(parents=True, exist_ok=True)
token_path.write_text(token)
```

### Storage

```
$MNG_AGENT_STATE_DIR/
└── plugin/
    └── default_url_for_cli_agents_via_ttyd/
        ├── token           # The secret token (43 chars, URL-safe base64)
        ├── port            # ttyd port number
        └── pid             # ttyd process ID
```

## URL Generation

On agent creation, after ttyd starts:

1. Pick an available port from configured range (default: 7680-7780)
2. Start ttyd on that port
3. Call `forward-service add --name terminal --port <ttyd-port>`
4. Read back the forwarded URL
5. Append `?arg=<token>` to the URL
6. Store in agent state:
   - `plugin.default_url_for_cli_agents_via_ttyd.url` - Full URL with token
   - `plugin.default_url_for_cli_agents_via_ttyd.port` - Local ttyd port
   - `plugin.default_url_for_cli_agents_via_ttyd.pid` - ttyd process ID
7. If agent has no `url` field set, set it to this URL

### URL Format

```
http://terminal.<agent>.<host>.mng.localhost:8080/?arg=<token>
```

## Agent Fields

This plugin adds fields via `agent_field_generators` hook:

| Field | Type | Description |
|-------|------|-------------|
| `plugin.default_url_for_cli_agents_via_ttyd.url` | string | Full URL with token |
| `plugin.default_url_for_cli_agents_via_ttyd.port` | int | Local ttyd port |
| `plugin.default_url_for_cli_agents_via_ttyd.pid` | int | ttyd process ID |

If the agent's `url` field is unset, this plugin sets it to the ttyd URL.

## Lifecycle

### Agent Creation

1. `on_after_agent_create` hook fires
2. Generate and store token
3. Find available port in range
4. Start ttyd process
5. Store PID
6. Call forward-service to expose
7. Compute and store full URL

### Agent Destruction

1. `on_before_agent_destroy` hook fires
2. Kill ttyd process (if running)
3. Call `forward-service remove --name terminal`
4. Clean up plugin state directory

## Dependencies

Requires `forward-service` command from [local_port_forwarding_via_frp_and_nginx](./local_port_forwarding_via_frp_and_nginx.md) or compatible plugin.

On missing dependency, agent creation fails.
