# Workspace template documentation

A "workspace" is a persistent mngr agent created from a template repository. The template defines the agent's entire runtime environment.

## Template structure

The template repository (e.g. [default-workspace-template](https://github.com/imbue-ai/default-workspace-template)) contains:

- `.mngr/settings.toml` -- mngr configuration: agent types, create templates, environment variables
- `system/supervisord.conf` -- background services, each a `[program:*]` section supervised by supervisord
- `system/Dockerfile` -- container image definition
- `CLAUDE.md` -- instructions for the Claude agent
- `.agents/skills/` -- skills available to the agent
- `system/scripts/` -- utility scripts (forward_port.py, run_ttyd.sh, etc.)
- `system/libs/` -- Python packages for the built-in services (bootstrap, cloudflare_tunnel, app_watcher, ...); user-built packages live in `creations/`
- `data/` -- gitignored workspace data (uploads, memories, per-creation data, machine state, secrets)

## Key files

### system/supervisord.conf

Declares the background services as `[program:*]` sections that supervisord
starts and supervises (logs under `/var/log/supervisor`). The bootstrap runs
first-boot setup and then execs `supervisord -n -c system/supervisord.conf`:

```ini
[program:system_interface]
command=bash -c "python3 system/scripts/forward_port.py --url http://localhost:8000 --name system_interface && system-interface"
directory=/home/user/workspace
autostart=true
autorestart=true

[program:terminal]
command=bash system/scripts/run_ttyd.sh
directory=/home/user/workspace
autostart=true
autorestart=true

[program:cloudflared]
command=uv run cloudflare-tunnel
directory=/home/user/workspace
autostart=true
autorestart=true

[program:app-watcher]
command=uv run app-watcher
directory=/home/user/workspace
autostart=true
autorestart=true
```

### data/.state/applications.toml

Tracks application ports for forwarding. Written by services via `system/scripts/forward_port.py`:

```toml
[[applications]]
name = "web"
url = "http://localhost:8000"
global = true
```

### data/.secrets

Contains environment variable exports injected by the desktop client:

```bash
export CLOUDFLARE_TUNNEL_TOKEN=eyJ...
```

## How services register ports

Services call `system/scripts/forward_port.py` on startup to register their ports:

```bash
python3 system/scripts/forward_port.py --url http://localhost:8000 --name web
python3 system/scripts/forward_port.py --url http://localhost:7681 --name terminal
python3 system/scripts/forward_port.py --remove --name old-service
```

The app watcher service monitors `applications.toml` and:
1. Writes service events to `events/services/events.jsonl` for the desktop client to discover
2. Reconciles with the Cloudflare forwarding API (adds missing services, removes stale ones)
