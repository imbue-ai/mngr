# Mind template documentation

A "mind" is a persistent mngr agent created from a template repository. The template defines the agent's entire runtime environment.

## Template structure

The template repository (e.g. [forever-claude-template](https://github.com/imbue-ai/forever-claude-template)) contains:

- `.mngr/settings.toml` -- mngr configuration: agent types, create templates, environment variables
- `services.toml` -- background services managed by the bootstrap service manager
- `Dockerfile` -- container image definition
- `CLAUDE.md` -- instructions for the Claude agent
- `skills/` -- slash commands available to the agent
- `scripts/` -- utility scripts (forward_port.py, run_ttyd.sh, etc.)
- `libs/` -- Python packages for services (telegram_bot, bootstrap, cloudflare_tunnel, app_watcher)
- `runtime/` -- gitignored runtime state (applications.toml, secrets, telegram history)

## Key files

### services.toml

Defines background services that run in tmux windows:

```toml
[services.web]
command = "python3 scripts/forward_port.py --url http://localhost:8000 --name web && claude-web-chat"

[services.terminal]
command = "bash scripts/run_ttyd.sh"

[services.cloudflared]
command = "uv run cloudflare-tunnel"
restart = "on-failure"

[services.app-watcher]
command = "uv run app-watcher"
restart = "on-failure"
```

### runtime/applications.toml

Tracks application ports for forwarding. Written by services via `scripts/forward_port.py`:

```toml
[[applications]]
name = "web"
url = "http://localhost:8000"
global = true
```

### runtime/secrets

Contains environment variable exports injected by the forwarding server:

```bash
export CLOUDFLARE_TUNNEL_TOKEN=eyJ...
```

## How services register ports

Services call `scripts/forward_port.py` on startup to register their ports:

```bash
python3 scripts/forward_port.py --url http://localhost:8000 --name web
python3 scripts/forward_port.py --url http://localhost:7681 --name terminal
python3 scripts/forward_port.py --remove --name old-service
```

The app watcher service monitors `applications.toml` and:
1. Writes server events to `events/servers/events.jsonl` for the forwarding server to discover
2. Reconciles with the Cloudflare forwarding API (adds missing services, removes stale ones)
