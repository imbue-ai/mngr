# minds

Run persistent, autonomous AI agents with web access and global forwarding.

## Overview

The minds app creates and manages persistent Claude agents running in Docker containers. Each agent gets:

- A local web interface accessible through the desktop client
- Optional global access via Cloudflare tunnels (with Google OAuth protection)
- Background services (web server, terminal, etc.) supervised by supervisord
- The ability to expose application ports via both local and global URLs

## Getting started

minds ships as a desktop app (Electron, packaged via ToDesktop; see
[docs/desktop-app.md](./docs/desktop-app.md)). To run it from source in this
monorepo, activate a minds env and start the dev client:

```bash
eval "$(uv run minds env activate <name>)"   # e.g. dev-<your-user>
just minds-start
```

Then visit the login URL printed in the terminal to create your first agent.

## How it works

1. The **desktop client** (`minds run`) runs locally and provides:
   - Authentication via one-time login codes
   - A web UI for creating agents from template repositories
   - Reverse proxying to agent web servers (HTTP + WebSocket)
   - A servers page showing local and global URLs per agent
   - Toggle controls for enabling/disabling global Cloudflare forwarding

2. **Agents** are created from template repositories (like [forever-claude-template](https://github.com/imbue-ai/forever-claude-template)) using `mngr create`. The template's `.mngr/settings.toml` drives all configuration.

3. Inside each minds container, the "primary" agent (`system-services`) runs only the bootstrap and background services -- its window-0 command is `sleep infinity && claude`, so claude never actually starts (the trailing `&& claude` is unreachable; it exists only so `assemble_command` keeps producing a claude-shaped invocation). The user's actual chat agent is a separate `mngr` agent created by the bootstrap on first boot (named after the host) and shares the services agent's `CLAUDE_CONFIG_DIR` so auth, plugins, marketplaces, and sessions are configured once and inherited by every other agent. Destroying chat agents no longer affects services; the services agent is hidden from the UI agent list (it carries `is_primary=true`) and protected against direct destroy.

4. Inside the services agent's Docker container:
   - The bootstrap (`uv run bootstrap`) runs first-boot setup and then execs `supervisord -n`, which supervises the background services declared as `[program:*]` sections in `supervisord.conf`
   - On first boot the bootstrap also writes `CLAUDE_CONFIG_DIR` to the host env file and creates the initial chat agent (gated by `runtime/initial_chat_created`)
   - Services register their ports via `scripts/forward_port.py` into `runtime/applications.toml`
   - An **app watcher** service monitors `applications.toml` and writes server events to `events.jsonl` for discovery
   - A **cloudflared** service watches `runtime/secrets` for a tunnel token and runs the Cloudflare tunnel

## Learn more

- [Architecture and design](./docs/design.md)
- [Desktop client internals](./imbue/minds/desktop_client/README.md)
- [Glossary of key concepts](./docs/workspace/glossary.md)
- [Desktop app](./docs/desktop-app.md)
- [Latchkey permissions](./docs/latchkey-permissions.md)

## Testing live deployments

The `apps/minds/deployment_tests/` suite exercises real deployed minds services and the deploy process itself, driven by an operator-invoked orchestrator (`just minds-test-deployment`). See [`apps/minds/deployment_tests/README.md`](./deployment_tests/README.md) for the runbook and [`specs/minds-deployment-tests.md`](../../specs/minds-deployment-tests.md) for the full design.
