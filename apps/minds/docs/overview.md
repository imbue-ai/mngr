# How it works

Each workspace is a persistent `mngr` agent running in a Docker container, created from a template repository. The template defines everything the agent needs: services, skills, configuration, and a Dockerfile.

## Architecture

The system has two main components:

### Desktop client (runs on your machine)

The desktop client (`minds forward`) provides:
- Authentication via one-time codes and signed cookies
- A landing page listing all accessible workspaces (or a creation form if none exist)
- Agent creation from git repositories or local paths via a web form or API
- Byte-forwarding of HTTP and WebSocket traffic from `<agent-id>.localhost:8420/*` to the workspace's own `minds_workspace_server` (optionally through an SSH tunnel for remote agents)

Each workspace runs its own `minds_workspace_server`, which serves the dockview UI and multiplexes the workspace's services under `/service/<name>/...` paths (Service Worker bootstrap, HTML/cookie rewriting, and WebSocket shims live there, not in the desktop client). Browsers access a workspace at `http://<agent-id>.localhost:8420/` and its individual services at `http://<agent-id>.localhost:8420/service/<service_name>/`.

### Agent container (runs in Docker)

Inside each agent's Docker container:
- **Claude Code** runs as the main agent process in tmux window 0
- A **bootstrap service manager** watches `services.toml` and manages background services in tmux windows
- Services register their ports via `scripts/forward_port.py` into `runtime/applications.toml`
- An **app watcher** service monitors `applications.toml`, reconciles with the Cloudflare forwarding API, and writes service events to `events/services/events.jsonl`
- A **cloudflared** service watches `runtime/secrets` for a tunnel token and manages the Cloudflare tunnel
- A **telegram bot** watches for incoming messages and forwards them to the agent via `mngr message`

## Creating agents

Agents can be created in two ways:

1. **Via the web UI**: Visit the desktop client. If no agents exist, you'll see a creation form. Enter a git repository URL (or local path), agent name, and launch mode (DEV or LOCAL). The desktop client clones the repo (if URL), runs `mngr create` with the appropriate templates, creates a Cloudflare tunnel, and injects the tunnel token.

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
1. **Local**: `http://{agent_id}.localhost:8420/service/{service_name}/` (the desktop client byte-forwards the subdomain request to the workspace's `minds_workspace_server`, which serves the service under `/service/<name>/`)
2. **Global**: `https://{service}--{agent_id}--{username}.{domain}` (via Cloudflare tunnel)

The `global` flag indicates whether the agent wants Cloudflare forwarding enabled. The Share modal inside the workspace's dockview UI is authoritative for the actual state.

## Cloudflare tunnel integration

The remote service connector URL comes from `MindsConfig.remote_service_connector_url`, loaded from `~/.<MINDS_ROOT_NAME>/config.toml` or the `REMOTE_SERVICE_CONNECTOR_URL` environment variable (env overrides file), with a dev-deployed default baked in. Every tunnel request authenticates with the signed-in user's SuperTokens session -- no Basic-auth credentials or `OWNER_EMAIL` need to be configured on the client. Once signed in:

1. A tunnel is created automatically after each agent is created
2. The tunnel token is injected into the agent's `runtime/secrets`
3. The cloudflared service inside the agent detects the token and starts the tunnel
4. The app watcher registers services with the Cloudflare forwarding API
5. Access is protected by Cloudflare Access with a default policy for the signed-in user's email
