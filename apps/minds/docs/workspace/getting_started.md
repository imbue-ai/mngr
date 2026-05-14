# Getting started

## Starting the desktop client

```bash
minds run
```

This starts the local desktop client (default: `http://127.0.0.1:8420`). A one-time login URL is printed to the terminal.

## Creating your first agent

1. Open the login URL in your browser
2. You'll see the creation form (since no agents exist yet)
3. Fill in:
   - **Name**: a short identifier for the agent (e.g. "selene")
   - **Git repository**: URL or local path to a template repo (e.g. `https://github.com/imbue-ai/forever-claude-template`)
   - **Launch mode**: LOCAL (Docker container on this machine), LIMA (Lima VM), CLOUD (Docker on a Vultr VPS), or IMBUE_CLOUD (leased pool host via the imbue_cloud provider)
4. Click "Create" and wait for the Docker build + agent setup
5. You'll be redirected to the agent's web server when creation completes

## What happens during creation

1. The desktop client clones the repo (if URL) or uses it directly (if local path)
2. Runs `mngr create` with templates from the repo's `.mngr/settings.toml`
3. If Cloudflare is configured, creates a tunnel and injects the token
4. The agent starts in a tmux session with background services

## Accessing your agent

After creation, the agent is accessible at:
- **Local**: `http://{agent_id}.localhost:8420/` (the desktop client byte-forwards the subdomain to the workspace's workspace server, which serves the dockview UI)
- **Individual service**: `http://{agent_id}.localhost:8420/service/{service_name}/` (e.g. `.../service/web/`, `.../service/terminal/`)
- **Global** (if Cloudflare configured): `https://{service}--{agent_id}--{username}.{domain}`

## Environment variables and config

The remote service connector URL is taken from the per-tier `client.toml` selected by `minds run --config-file <path>` (see `apps/minds/docs/environments.md`). When `--config-file` is not passed, the default resolves to `apps/minds/imbue/minds/config/envs/_bundled/client.toml` (written by the Electron production build) and falls back to `apps/minds/imbue/minds/config/envs/dev/client.toml` shipped with the wheel. That URL hosts both the Cloudflare tunnel API and the `/auth/*` routes the desktop client uses for sign-in. All Cloudflare tunnel requests authenticate with the signed-in user's SuperTokens session, and the session's email is used as the default Cloudflare Access policy -- so no Basic-auth credentials or `OWNER_EMAIL` need to be configured on the client. SuperTokens credentials (API key, OAuth client secrets) live in HCP Vault (see `apps/minds/docs/vault-setup.md`) and are pushed into Modal Secrets at deploy time; they never need to be set on the client.

To pin a specific tier or a dynamic dev env, point `--config-file` at the desired TOML:

```bash
minds run --config-file apps/minds/imbue/minds/config/envs/staging/client.toml
# or a per-developer dynamic dev env:
minds run --config-file ~/.minds/envs/<dev-name>.toml
```

To run an isolated dev copy alongside an installed minds:

```bash
export MINDS_ROOT_NAME=devminds    # data lives in ~/.devminds/ with MNGR_PREFIX=devminds-
```

For agent-specific secrets (API keys, telegram credentials), set them in the template repo's `.env` file and ensure they're listed in `pass_env` in `.mngr/settings.toml`.
