---
name: create-mind-locally
description: Create a mind agent in a local Docker container from a template repo
---

# Creating a mind agent locally

Minds are created from template repos (like `forever-claude-template`) via the desktop client UI or the `mngr create` CLI.

## Via the desktop client (Electron app)

1. Start the app: `MIND_GIT_URL=/path/to/template MIND_NAME=myagent (cd apps/minds && pnpm start)`
2. The creation form is pre-filled with the git URL and name
3. Select launch mode: DEV (local, no Docker) or LOCAL (Docker container)
4. Click create and wait for the Docker build + agent startup

## Via CLI directly

```bash
# DEV mode (runs in-place, no Docker):
cd /path/to/template-repo
uv run mngr create myagent --no-connect --template main --template dev

# LOCAL mode (Docker container):
cd /path/to/template-repo
uv run mngr create myagent@myagent-host.docker --new-host --no-connect --template main --template docker
```

## What happens during creation

1. If the source is a git worktree, it's cloned to `/tmp/minds-clone-{repo_name}` (shallow, `--depth 1`) to produce a standalone repo Docker can copy
2. `mngr create` builds the Docker image from the template's Dockerfile
3. The agent starts inside the container with tmux, bootstrap services, etc.
4. The desktop client discovers the agent via `mngr observe` and starts streaming server events via `mngr events`
5. The web server URL (usually `http://localhost:8000`) is proxied through an SSH tunnel

## Destroying and recreating

```bash
# Destroy agent and container:
uv run mngr destroy -f myagent

# If the container name conflicts on next create:
docker rm -f mngr-myagent-host
```

## Key files in the template repo

| File | Purpose |
|------|---------|
| `.mngr/settings.toml` | Agent types, create templates, env vars, plugin config |
| `Dockerfile` | Docker image definition |
| `services.toml` | Background services (bootstrap, web server, telegram, etc.) |
| `CLAUDE.md` | Agent instructions |

## Template repo requirements for the web chat UI

The template's Dockerfile must:
- Install Node.js and build the `claude-web-chat` frontend (`npm ci && npm run build` in `vendor/mngr/apps/claude_web_chat/frontend/`)
- Install `claude-web-chat` as a uv tool with mngr plugin packages (`--with` mngr_claude, mngr_modal)

The template's `services.toml` must define a `web` service that runs `claude-web-chat` on port 8000.
