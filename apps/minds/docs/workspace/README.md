# Workspace template documentation

A "workspace" is a persistent mngr agent created from a template repository. The template defines the agent's entire runtime environment.

## Template structure

The template repository (e.g. [default-workspace-template](https://github.com/imbue-ai/default-workspace-template)) contains:

- `.mngr/settings.toml` -- mngr configuration: agent types, create templates, environment variables
- `system/supervisord.conf` -- the apps' and background services' `[program:*]` sections, supervised by supervisord
- `system/Dockerfile` -- container image definition
- `CLAUDE.md` -- instructions for the Claude agent
- `.agents/skills/` -- skills available to the agent
- `system/scripts/` -- utility scripts (forward_port.py, layout.py, etc.)
- `system/apps/` -- everything tab-openable (system_interface, terminal, browser, and user-built apps); `system/services/` -- tab-less background services (app_watcher, cloudflare_tunnel, host_backup, ...); `system/libs/` -- support libraries (bootstrap, ...)
- `data/` -- gitignored workspace data (documents, uploads, memories, per-app data, machine state, secrets)

## Key files

### system/supervisord.conf

Declares the apps and background services as `[program:*]` sections that supervisord
starts and supervises (logs under `/var/log/supervisor`). The bootstrap runs
first-boot setup and then execs `supervisord -n -c system/supervisord.conf`:

```ini
[program:system_interface]
command=bash -c "python3 system/scripts/forward_port.py --url http://localhost:8000 --name system_interface && system-interface"
directory=/home/user/workspace
autostart=true
autorestart=true

[program:terminal]
command=bash system/apps/terminal/run_ttyd.sh
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

### data/.state/apps.toml

Tracks app ports for forwarding. Written by apps via `system/scripts/forward_port.py`:

```toml
[[apps]]
name = "web"
url = "http://localhost:8000"
global = true
```

### data/.secrets

Contains environment variable exports injected by the desktop client:

```bash
export CLOUDFLARE_TUNNEL_TOKEN=eyJ...
```

## How apps register ports

Apps call `system/scripts/forward_port.py` on startup to register their ports:

```bash
python3 system/scripts/forward_port.py --url http://localhost:8000 --name web
python3 system/scripts/forward_port.py --url http://localhost:7681 --name terminal
python3 system/scripts/forward_port.py --remove --name old-app
```

The app watcher service monitors `apps.toml` and writes service events to `events/services/events.jsonl` for the desktop client to discover. (Cloudflare forwarding registration happens on the minds side when the user enables sharing -- not in the watcher.)
