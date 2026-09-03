# How it works

Each workspace is a persistent `mngr` agent running in a Docker container, created from a template repository. The template defines everything the agent needs: apps, services, skills, configuration, and a Dockerfile.

## Architecture

The system has two main components:

### Desktop client (runs on your machine)

The desktop client (`minds run`) provides:
- Authentication via one-time codes and signed cookies
- A landing page listing all accessible workspaces (or a creation form if none exist). Shutdown-capable minds (the local `docker` / `lima` backends and the cloud `aws` / `gcp` / `azure` / `imbue_cloud` ones) show a live container-status badge and a Start/Stop button (Stop asks for confirmation); the status comes from the discovery snapshot's host state (a user-issued Start/Stop flips it immediately via an optimistic override). The same liveness, narrowed to local minds, drives the quit-time shutdown prompt (see `desktop-app.md`).
- Agent creation from git repositories or local paths via a web form or API
- Byte-forwarding of HTTP and WebSocket traffic from `[<service>.]host-<hex>.localhost:8421/*` to the workspace's own backends: the bare origin reaches the system interface (the `system-interface` CLI, source at `default-workspace-template/system/apps/system_interface/`), `<service>.` origins reach that registered service (optionally through an SSH tunnel for remote agents)

Each workspace runs its own system interface (the `system-interface` CLI, source at `default-workspace-template/system/apps/system_interface/`), which serves the dockview UI at the workspace's bare origin. Every other registered service owns its own origin, so nothing proxies or rewrites service traffic. Browsers access a workspace at `https://host-<hex>.localhost:8421/` and its individual services at `https://<service_name>.host-<hex>.localhost:8421/`.

### Agent container (runs in Docker)

Inside each agent's Docker container:
- **Claude Code** runs as the main agent process in tmux window 0
- The **bootstrap** (`uv run bootstrap`) runs first-boot setup and then execs `supervisord -n`, which supervises the background services declared as `[program:*]` sections in `supervisord.conf` (logs under `/var/log/supervisor`)
- Apps register their ports via `system/scripts/forward_port.py` into `data/.state/apps.toml`
- An **app watcher** service monitors `apps.toml` and writes service events to `events/services/events.jsonl` for discovery
- A **share-gateway** service watches `data/.secrets/share.env` for relay materials and runs the workspace's share stack (relay tunnel + in-workspace TLS) while sharing is enabled
- A **telegram bot** watches for incoming messages and forwards them to the agent via `mngr message`

## Creating agents

Agents can be created in two ways:

1. **Via the web UI**: Visit the desktop client. If no agents exist, you'll see a creation form. Enter a git repository URL (or local path), agent name, and launch mode (DOCKER, LIMA, CLOUD, or IMBUE_CLOUD). The desktop client clones the repo (if URL) and runs `mngr create` with the appropriate templates. Sharing is machine-level and user-initiated, so nothing sharing-related happens at create time.

2. **Via the API**: POST to `/api/create-agent` with a JSON body containing `git_url`, `agent_name`, and `launch_mode`. Poll `/api/create-agent/{agent_id}/status` for progress.

## Port forwarding

Apps (tab-openable, with forwarded ports) are tracked in `data/.state/apps.toml`:

```toml
[[apps]]
name = "web"
url = "http://localhost:8000"
```

Each app gets two URLs:
1. **Local**: `https://{service_name}.{host_id}.localhost:8421/` (the desktop client byte-forwards the service-origin request straight to the registered service's backend)
2. **Shared**: `https://[{service}.]{host_id}.{user}.{region}.{domain}` (over the workspace's share, while sharing is enabled)

The Share modal inside the workspace's dockview UI is authoritative for the actual sharing state.

## Workspace sharing

The remote service connector URL comes from the per-tier `client.toml` loaded via `minds run --config-file <path>` (see `apps/minds/docs/deploy/reference/environments.md`). `minds run` has no implicit default: if neither `--config-file` nor `MINDS_CLIENT_CONFIG_PATH` is set it refuses to start. The packaged Electron build passes `--config-file` explicitly from the bundled `client.toml`. Every share request authenticates with the signed-in user's SuperTokens session -- no Basic-auth credentials or `OWNER_EMAIL` need to be configured on the client.

Sharing is machine-level: when the user enables it for a workspace, the desktop client calls `mngr imbue_cloud shares create` (which registers the share with the connector and returns the relay coordinates + one-time relay token) and injects those materials into the agent's `data/.secrets/share.env`. The share-gateway service inside the workspace then dials the self-hosted relay, obtains a real TLS certificate, and terminates TLS inside the workspace; access is gated by the grants document (`data/.secrets/share_grants.toml`), which the desktop client rewrites in place as the user edits grants.
