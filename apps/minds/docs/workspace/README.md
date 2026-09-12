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
- `system/apps/` -- everything tab-openable (system_interface, chat, terminal, browser, and user-built apps); `system/services/` -- tab-less background services (app_watcher, share_gateway, host_backup, ...); `system/libs/` -- support libraries (bootstrap, ...)
- `data/` -- gitignored workspace data (documents, uploads, memories, per-app data, machine state, secrets)

## Key files

### system/supervisord.conf

Declares the apps and background services as `[program:*]` sections that supervisord
starts and supervises (logs under `/var/log/supervisor`). The bootstrap runs
first-boot setup and then execs `supervisord -n -c system/supervisord.conf`:

```ini
[program:system_interface]
command=bash -c "python3 system/scripts/forward_port.py --manifest system/apps/system_interface/app.toml --url http://localhost:8000 && system-interface"
directory=/home/user/workspace
autostart=true
autorestart=true

[program:chat]
command=chat-app
directory=/home/user/workspace
autostart=true
autorestart=true

[program:terminal]
command=terminal-app
directory=/home/user/workspace
autostart=true
autorestart=true

[program:share-gateway]
command=uv run share-gateway
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

Contains environment variable exports injected by the desktop client.
While sharing is enabled, `data/.secrets/share.env` holds the share
materials the share-gateway service watches for (alongside
`data/.secrets/share_grants.toml`, the grants document gating access):

```bash
export SHARE_WORKSPACE_DOMAIN=host-<hex>.<user>.us1.example.com
export SHARE_RELAY_TOKEN=...
export SHARE_CONNECTOR_URL=https://...
export SHARE_BROKER_URL=https://...
```

Note that share.env carries no relay endpoint: the share-gateway fetches
its current relay set from the connector's `GET /shares/assignment`
endpoint (authenticated by the relay token) and re-polls it, so relay
fleet changes never require re-injecting materials.

## Sandboxed runtime on remote workspaces

Remote (imbue_cloud) workspaces run their container under gVisor (`runsc`, a
user-space kernel between the container and the VM's kernel; `uname -r` inside
the container reports `4.19.0-gvisor`), with `/run` and `/tmp` on tmpfs.
Software that needs ptrace tooling (`strace`, `gdb` attach, `perf`), eBPF,
FUSE, `io_uring`, nested container runtimes, or unusual `ioctl`s does not work
inside the sandbox, and filesystem-metadata-heavy operations (`find`, `tar`,
`git status` over large trees) are several times slower than on a plain kernel.
The template's `CLAUDE.md` / `AGENTS.md` tell the agent the same.

## How apps register ports

Apps call `system/scripts/forward_port.py` on startup to register their ports. An app with
an `app.toml` manifest registers through `--manifest` and takes its name from the manifest;
`--name` names a row that has no manifest of its own (an extra origin-label row of a
multi-port app, or a hand-written block):

```bash
python3 system/scripts/forward_port.py --manifest system/apps/web/app.toml --url http://localhost:8000
python3 system/scripts/forward_port.py --url http://localhost:8001 --name web-admin
python3 system/scripts/forward_port.py --remove --name old-app
```

The app watcher service monitors `apps.toml` and writes service events to `events/services/events.jsonl` for the desktop client to discover. (Share registration happens on the minds side when the user enables sharing -- not in the watcher.)
