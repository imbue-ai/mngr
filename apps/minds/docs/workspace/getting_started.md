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
   - **Launch mode**: LOCAL (Docker container) or DEV (runs in-place)
4. Click "Create" and wait for the Docker build + agent setup
5. You'll be redirected to the agent's web server when creation completes

## What happens during creation

1. The desktop client clones the repo (if URL) or uses it directly (if local path)
2. Runs `mngr create` with templates from the repo's `.mngr/settings.toml`
3. If Cloudflare is configured, creates a tunnel and injects the token
4. The agent starts in a tmux session with background services

## Accessing your agent

After creation, the agent is accessible at:
- **Local**: `http://{agent_id}.localhost:8420/` (the desktop client byte-forwards the subdomain to the workspace's `minds_workspace_server`, which serves the dockview UI)
- **Individual service**: `http://{agent_id}.localhost:8420/service/{service_name}/` (e.g. `.../service/web/`, `.../service/terminal/`)
- **Global** (if Cloudflare configured): `https://{service}--{agent_id}--{username}.{domain}`

## Environment variables and config

`REMOTE_SERVICE_CONNECTOR_URL` comes from `MindsConfig`: a default is baked in pointing at the current dev-deployed server, and you can override it via `~/.<MINDS_ROOT_NAME>/config.toml` (file) or environment variable (env overrides file). That URL hosts both the Cloudflare tunnel API and the `/auth/*` routes the desktop client uses for sign-in, so no env-var setup is required for default operation. All Cloudflare tunnel requests authenticate with the signed-in user's SuperTokens session, and the session's email is used as the default Cloudflare Access policy -- so no Basic-auth credentials or `OWNER_EMAIL` need to be configured on the client. SuperTokens credentials (API key, OAuth client secrets) live on the backend server and never need to be set on the client.

To pin the remote service connector URL explicitly:

```bash
export REMOTE_SERVICE_CONNECTOR_URL=https://your-modal-endpoint.modal.run
```

To run an isolated dev copy alongside an installed minds:

```bash
export MINDS_ROOT_NAME=devminds    # data lives in ~/.devminds/ with MNGR_PREFIX=devminds-
```

For agent-specific secrets (API keys, telegram credentials), set them in the template repo's `.env` file and ensure they're listed in `pass_env` in `.mngr/settings.toml`.
