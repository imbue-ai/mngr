# How it works

Each workspace is a persistent `mngr` agent running in a Docker container, created from a template repository. The template defines everything the agent needs: apps, services, skills, configuration, and a Dockerfile.

## Architecture

The system has two main components:

### Desktop client (runs on your machine)

The desktop client (`minds run`) provides:
- Authentication via one-time codes and signed cookies
- A landing page listing all accessible workspaces (or a creation form if none exist). Local (`docker` / `lima`) minds show a live container-status badge and a Start/Stop button (Stop asks for confirmation); the status comes from the discovery snapshot's host state (a user-issued Start/Stop flips it immediately via an optimistic override), and the same liveness drives the quit-time shutdown prompt (see `desktop-app.md`).
- Agent creation from git repositories or local paths via a web form or API
- Byte-forwarding of HTTP and WebSocket traffic for the workspace origins — the bare `<agent-id>.localhost` origin to the shell (the `system-interface` CLI, source at `default-workspace-template/system/apps/system_interface/`) and `<service>.<agent-id>.localhost` to each registered service's port (optionally through an SSH tunnel for remote agents)

Each workspace runs its own system interface (the `system-interface` CLI, source at `default-workspace-template/system/apps/system_interface/`), which serves the dockview shell UI. Every registered service owns its own browser origin: the shell lives at the bare `https://<agent-id>.localhost:8421/` origin and each service at `https://<service_name>.<agent-id>.localhost:8421/` (deeper subdomains route to the same service, so multi-origin apps work unmodified). Routing is byte-level and Host-header-based in the `mngr forward` plugin; nothing proxies, rewrites, or shims app traffic.

### Agent container (runs in Docker)

Inside each agent's Docker container:
- **Claude Code** runs as the main agent process in tmux window 0
- The **bootstrap** (`uv run bootstrap`) runs first-boot setup and then execs `supervisord -n`, which supervises the background services declared as `[program:*]` sections in `supervisord.conf` (logs under `/var/log/supervisor`)
- Apps register their ports via `system/scripts/forward_port.py` into `data/.state/apps.toml`
- An **app watcher** service monitors `apps.toml` and writes service events to `events/services/events.jsonl` for discovery
- A **cloudflared** service watches `data/.secrets` for a tunnel token and manages the Cloudflare tunnel
- A **telegram bot** watches for incoming messages and forwards them to the agent via `mngr message`

## Creating agents

Agents can be created in two ways:

1. **Via the web UI**: Visit the desktop client. If no agents exist, you'll see a creation form. Enter a git repository URL (or local path), agent name, and launch mode (DOCKER, LIMA, CLOUD, or IMBUE_CLOUD). The desktop client clones the repo (if URL), runs `mngr create` with the appropriate templates, creates a Cloudflare tunnel, and injects the tunnel token.

2. **Via the API**: POST to `/api/create-agent` with a JSON body containing `git_url`, `agent_name`, and `launch_mode`. Poll `/api/create-agent/{agent_id}/status` for progress.

## Port forwarding

Apps (tab-openable, with forwarded ports) are tracked in `data/.state/apps.toml`:

```toml
[[apps]]
name = "web"
url = "http://localhost:8000"
```

Each app gets two URLs (two spellings of the same service coordinate):
1. **Local**: `https://{service_name}.{agent_id}.localhost:8421/` (the `mngr forward` plugin routes the service subdomain straight to the app's registered port; service names must be DNS-safe labels)
2. **Shared**: `https://{service_name}--{host}--{user}.{domain}` (via Cloudflare tunnel, once the workspace or that service is shared)

Every registered service is exposed when the workspace is shared; sharing is configured from the workspace settings in the desktop client.

## Cloudflare tunnel integration

The remote service connector URL comes from the per-tier `client.toml` loaded via `minds run --config-file <path>` (see `apps/minds/docs/environments.md`). `minds run` has no implicit default: if neither `--config-file` nor `MINDS_CLIENT_CONFIG_PATH` is set it refuses to start. The packaged Electron build passes `--config-file` explicitly from the bundled `client.toml`. Every tunnel request authenticates with the signed-in user's SuperTokens session -- no Basic-auth credentials or `OWNER_EMAIL` need to be configured on the client. Once signed in:

1. A tunnel is created automatically after each agent is created
2. The tunnel token is injected into the agent's `data/.secrets`
3. The cloudflared service inside the agent detects the token and starts the tunnel
4. When the user enables sharing for a service, the desktop client registers it with the Cloudflare forwarding API (via `mngr imbue_cloud tunnels enable-sharing`)
5. Access is protected by Cloudflare Access with a default policy for the signed-in user's email
