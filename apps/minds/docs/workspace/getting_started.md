# Getting started

## Starting the desktop client

In normal use, launch the Electron app -- either the packaged build or
`just minds-start` from this repo root for development iteration.
Electron spawns the `minds run` backend internally (default:
`http://127.0.0.1:8420`); a one-time login URL is printed to the
terminal and the system browser opens directly on that URL.

Before running `just minds-start` (or invoking `minds run` directly
from source), **activate an env in your shell**:

```bash
eval "$(uv run minds-admin env activate dev-<your-user>)"   # or `staging`, `production`
just minds-start
```

Activation exports the four env vars (`MINDS_ROOT_NAME`,
`MNGR_HOST_DIR`, `MNGR_PREFIX`, `MINDS_CLIENT_CONFIG_PATH`) that
point the backend at the env's `~/.minds-<env-name>/` data root and
the env's `client.toml`. Source runs refuse to start without
activation -- there is no implicit default.

To bypass Electron and exercise the backend on its own:

```bash
minds run
```

## Creating your first agent

1. Open the login URL in your browser
2. You'll see the creation form (since no agents exist yet)
3. Fill in:
   - **Name**: a short identifier for the agent (e.g. "selene")
   - **Git repository**: URL or local path to a template repo (e.g. `https://github.com/imbue-ai/default-workspace-template`)
   - **Launch mode**: DOCKER (Docker container on this machine), LIMA (Lima VM), CLOUD (Docker on a Vultr VPS), or IMBUE_CLOUD (leased pool host via the imbue_cloud provider)
4. Click "Create" and wait for the Docker build + agent setup
5. You'll be redirected to the agent's web server when creation completes

## What happens during creation

1. The desktop client clones the repo (if URL) or uses it directly (if local path)
2. Runs `mngr create` with templates from the repo's `.mngr/settings.toml`
3. The agent starts in a tmux session with its apps and background services

Nothing sharing-related happens at create time: sharing is machine-level
and user-initiated later, from the workspace options panel's Share tab.

## Accessing your agent

After creation, the agent is accessible at:
- **Local**: `https://host-{hex}.localhost:8421/` (the desktop client byte-forwards the bare workspace origin to the workspace's system interface, which serves the dockview UI)
- **Individual app**: `https://{app_name}.host-{hex}.localhost:8421/` (every registered service owns its own origin; nothing proxies or rewrites service traffic)
- **Shared** (while sharing is enabled): `https://[{service}.]{host-id}.{user}.{region}.{domain}`, served over the workspace's share through the self-hosted relay

## Environment variables and config

The remote service connector URL is taken from the per-env
`client.toml` that `minds-admin env activate` pointed `MINDS_CLIENT_CONFIG_PATH`
at (see `apps/minds/docs/deploy/reference/environments.md`). That URL hosts both the
share endpoints and the `/auth/*` routes the desktop client uses
for sign-in. Every share request authenticates with the signed-in
user's SuperTokens session, and who may access a share is controlled
by its grants document -- so no Basic-auth credentials or
`OWNER_EMAIL` need to be configured on the client. SuperTokens
credentials (API key, OAuth client secrets) live in HCP Vault (see
`apps/minds/docs/deploy/setup/vault.md`) and are pushed into Modal Secrets at
deploy time; they never need to be set on the client.

To switch envs, run `minds-admin env activate <name>` in your shell. The
activation sets `MINDS_CLIENT_CONFIG_PATH` for you -- you don't need
to pass `--config-file` manually:

```bash
# Activate a tier (staging or production):
eval "$(uv run minds-admin env activate staging)"
just minds-start

# Or a per-developer dev env:
eval "$(uv run minds-admin env activate dev-<your-user>)"
just minds-start

# Backend-only invocation (no Electron):
eval "$(uv run minds-admin env activate dev-<your-user>)"
minds run
```

To deactivate (clear the env vars from your shell):

```bash
eval "$(uv run minds-admin env deactivate)"
```

For agent-specific secrets (API keys, telegram credentials), set them in the template repo's `.env` file and ensure they're listed in `pass_env` in `.mngr/settings.toml`.
