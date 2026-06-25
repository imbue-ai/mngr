# How it works

Each workspace is a persistent `mngr` agent running in a Docker container, created from a template repository. The template defines everything the agent needs: services, skills, configuration, and a Dockerfile.

## Architecture

The system has two main components:

### Desktop client (runs on your machine)

The desktop client (`minds run`) provides:
- Authentication via one-time codes and signed cookies
- A landing page listing all accessible workspaces (or a creation form if none exist). Local (`docker` / `lima`) minds show a live container-status badge and a Start/Stop button (Stop asks for confirmation); the status comes from the discovery snapshot's host state (a user-issued Start/Stop flips it immediately via an optimistic override), and the same liveness drives the quit-time shutdown prompt (see `desktop-app.md`).
- Agent creation from git repositories or local paths via a web form or API
- Byte-forwarding of HTTP and WebSocket traffic from `<agent-id>.localhost:8420/*` to the workspace's own system interface (the `system-interface` CLI, source at `forever-claude-template/apps/system_interface/`; optionally through an SSH tunnel for remote agents)

Each workspace runs its own system interface (the `system-interface` CLI, source at `forever-claude-template/apps/system_interface/`), which serves the dockview UI and multiplexes the workspace's services under `/service/<name>/...` paths (Service Worker bootstrap, HTML/cookie rewriting, and WebSocket shims live there, not in the desktop client). Browsers access a workspace at `http://<agent-id>.localhost:8420/` and its individual services at `http://<agent-id>.localhost:8420/service/<service_name>/`.

### Agent container (runs in Docker)

Inside each agent's Docker container:
- **Claude Code** runs as the main agent process in tmux window 0
- The **bootstrap** (`uv run bootstrap`) runs first-boot setup and then execs `supervisord -n`, which supervises the background services declared as `[program:*]` sections in `supervisord.conf` (logs under `/var/log/supervisor`)
- Services register their ports via `scripts/forward_port.py` into `runtime/applications.toml`
- An **app watcher** service monitors `applications.toml`, reconciles with the Cloudflare forwarding API, and writes service events to `events/services/events.jsonl`
- A **cloudflared** service watches `runtime/secrets` for a tunnel token and manages the Cloudflare tunnel
- A **telegram bot** watches for incoming messages and forwards them to the agent via `mngr message`

## Creating agents

Agents can be created in two ways:

1. **Via the web UI**: Visit the desktop client. If no agents exist, you'll see a creation form. Enter a git repository URL (or local path), agent name, and launch mode (DOCKER, LIMA, CLOUD, or IMBUE_CLOUD). The desktop client clones the repo (if URL), runs `mngr create` with the appropriate templates, creates a Cloudflare tunnel, and injects the tunnel token.

2. **Via the API**: POST to `/api/create-agent` with a JSON body containing `git_url`, `agent_name`, and `launch_mode`. Poll `/api/create-agent/{agent_id}/status` for progress.

## Port forwarding

Applications (services with ports) are tracked in `runtime/applications.toml`:

```toml
[[applications]]
name = "web"
url = "http://localhost:8000"
global = true
```

Each application gets two URLs:
1. **Local**: `http://{agent_id}.localhost:8420/service/{service_name}/` (the desktop client byte-forwards the subdomain request to the workspace's system interface, which serves the service under `/service/<name>/`)
2. **Global**: `https://{service}--{agent_id}--{username}.{domain}` (via Cloudflare tunnel)

The `global` flag indicates whether the agent wants Cloudflare forwarding enabled. The Share modal inside the workspace's dockview UI is authoritative for the actual state.

## Cloudflare tunnel integration

The remote service connector URL comes from the per-tier `client.toml` loaded via `minds run --config-file <path>` (see `apps/minds/docs/environments.md`). `minds run` has no implicit default: if neither `--config-file` nor `MINDS_CLIENT_CONFIG_PATH` is set it refuses to start. The packaged Electron build passes `--config-file` explicitly from the bundled `client.toml`. Every tunnel request authenticates with the signed-in user's SuperTokens session -- no Basic-auth credentials or `OWNER_EMAIL` need to be configured on the client. Once signed in:

1. A tunnel is created automatically after each agent is created
2. The tunnel token is injected into the agent's `runtime/secrets`
3. The cloudflared service inside the agent detects the token and starts the tunnel
4. The app watcher registers services with the Cloudflare forwarding API
5. Access is protected by Cloudflare Access with a default policy for the signed-in user's email
