---
name: run-electron-dev
description: Run the minds Electron desktop app in development mode
---

# Running the Electron app in dev mode

In dev mode, the Electron app uses the monorepo workspace venv directly (no separate `uv sync`), so all mngr plugins and local code changes are picked up immediately on restart.

## Prerequisites

- Node.js >= 20, pnpm >= 10
- The monorepo venv must be set up (`uv sync --all-packages` from repo root)

## Command

```bash
# Basic launch:
(cd apps/minds && pnpm install && pnpm start)

# With default template repo and agent name pre-filled:
MIND_GIT_URL=/home/rtard/project/forever-claude-template \
MIND_NAME=forever \
(cd apps/minds && pnpm start)

# If using a worktree of the template repo:
MIND_GIT_URL=/path/to/worktree \
MIND_NAME=forever \
(cd apps/minds && pnpm start)
```

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `MIND_GIT_URL` | Default git URL or local path for the creation form | `https://github.com/imbue-ai/simple_mind.git` |
| `MIND_NAME` | Default agent name | `selene` |
| `MIND_BRANCH` | Default branch to checkout | `main` |

## How it works

1. Electron creates a window showing a loading screen
2. In dev mode, `uv sync` is skipped entirely
3. Electron spawns `uv run --package minds mind -vv --format jsonl forward --host 127.0.0.1 --port <port> --no-browser` from the monorepo root
4. Backend stderr is piped to the terminal (visible in the `pnpm start` terminal)
5. Backend emits a JSONL `login_url` event on stdout
6. Electron navigates to the login URL

## Iterating on code changes

- Python code changes: restart the Electron app (close window, re-run `pnpm start`)
- Electron JS changes: same -- restart the app
- Service worker changes (`proxy.py`): also clear the SW cache in DevTools > Application > Service Workers > Unregister
- `pnpm build` is NOT needed in dev mode (it downloads uv/git binaries for packaged builds only)

## Common issues

- **"Backend connection lost"**: check the terminal for WARNING-level errors. Usually means the SSH tunnel to the Docker container dropped or the web server inside isn't ready yet.
- **Service worker caching stale code**: open DevTools (Ctrl+Shift+I) > Application > Service Workers > Unregister, then reload.
- **Stale mngr processes**: if you see multiple `mngr observe` processes from previous runs, kill them: `pkill -f "mngr observe --discovery-only"`
