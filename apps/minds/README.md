# minds

Run persistent, autonomous AI agents with web access and global forwarding.

## Overview

The minds app creates and manages persistent Claude agents running in Docker containers. Each agent gets:

- A local web interface accessible through the desktop client
- Optional global access via Cloudflare tunnels (with Google OAuth protection)
- Background services (web server, terminal, telegram bot, etc.) managed by a bootstrap service manager
- The ability to expose application ports via both local and global URLs

## Getting started

```bash
# Install
curl -fsSL https://raw.githubusercontent.com/imbue-ai/mngr/main/apps/minds/scripts/install.sh | bash

# Start the desktop client
mind forward

# Visit the URL printed in the terminal to create your first agent
```

## How it works

1. The **desktop client** (`mind forward`) runs locally and provides:
   - Authentication via one-time login codes
   - A web UI for creating agents from template repositories
   - Reverse proxying to agent web servers (HTTP + WebSocket)
   - A servers page showing local and global URLs per agent
   - Toggle controls for enabling/disabling global Cloudflare forwarding

2. **Agents** are created from template repositories (like [forever-claude-template](https://github.com/imbue-ai/forever-claude-template)) using `mngr create`. The template's `.mngr/settings.toml` drives all configuration.

3. Inside each agent's Docker container:
   - A **bootstrap service manager** watches `services.toml` and starts/stops tmux windows for each service
   - Services register their ports via `scripts/forward_port.py` into `runtime/applications.toml`
   - An **app watcher** service monitors `applications.toml` and writes server events to `events.jsonl` for discovery
   - A **cloudflared** service watches `runtime/secrets` for a tunnel token and runs the Cloudflare tunnel

## Learn more

- [Architecture and design](./docs/design.md)
- [Desktop client internals](./imbue/minds/desktop_client/README.md)
- [Glossary of key concepts](./docs/mind/glossary.md)
- [Desktop app](./docs/desktop-app.md)
