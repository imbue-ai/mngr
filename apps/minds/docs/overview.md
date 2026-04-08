# How it works

Each mind is a persistent `mngr` agent running in a Docker container, created from a template repository. The template defines everything the agent needs: services, skills, configuration, and a Dockerfile.

## Architecture

The system has two main components:

### Forwarding server (runs on your machine)

The forwarding server (`mind forward`) provides:
- Authentication via one-time codes and signed cookies
- A landing page listing all accessible minds (or a creation form if none exist)
- Agent creation from git repositories or local paths via a web form or API
- Reverse proxying of HTTP and WebSocket traffic to agent web servers
- A per-agent servers page showing local and global (Cloudflare) URLs with toggle controls
- Service Worker-based path rewriting for transparent URL multiplexing

Each mind may run multiple web servers on separate ports. The forwarding server multiplexes access to all of them under path prefixes (e.g. `/agents/{agent_id}/{server_name}/`).

### Agent container (runs in Docker)

Inside each agent's Docker container:
- **Claude Code** runs as the main agent process in tmux window 0
- A **bootstrap service manager** watches `services.toml` and manages background services in tmux windows
- Services register their ports via `scripts/forward_port.py` into `runtime/applications.toml`
- An **app watcher** service monitors `applications.toml`, reconciles with the Cloudflare forwarding API, and writes server events to `events/servers/events.jsonl`
- A **cloudflared** service watches `runtime/secrets` for a tunnel token and manages the Cloudflare tunnel
- A **telegram bot** watches for incoming messages and forwards them to the agent via `mngr message`

## Creating agents

Agents can be created in two ways:

1. **Via the web UI**: Visit the forwarding server. If no agents exist, you'll see a creation form. Enter a git repository URL (or local path), agent name, and launch mode (DEV or LOCAL). The forwarding server clones the repo (if URL), runs `mngr create` with the appropriate templates, creates a Cloudflare tunnel, and injects the tunnel token.

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
1. **Local**: `http://localhost:8420/agents/{agent_id}/{server_name}/` (via forwarding server)
2. **Global**: `https://{service}--{agent_id}--{username}.{domain}` (via Cloudflare tunnel)

The `global` flag indicates whether the agent wants Cloudflare forwarding enabled. The forwarding server's toggle controls are authoritative for the actual state.

## Cloudflare tunnel integration

When the forwarding server has Cloudflare credentials configured (env vars `CLOUDFLARE_FORWARDING_URL`, `CLOUDFLARE_FORWARDING_USERNAME`, `CLOUDFLARE_FORWARDING_SECRET`, `OWNER_EMAIL`):

1. A tunnel is created automatically after each agent is created
2. The tunnel token is injected into the agent's `runtime/secrets`
3. The cloudflared service inside the agent detects the token and starts the tunnel
4. The app watcher registers services with the Cloudflare forwarding API
5. Access is protected by Cloudflare Access with a default Google OAuth policy for the owner's email
