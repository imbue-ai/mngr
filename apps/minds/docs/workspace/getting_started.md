# Getting started

## Starting the desktop client

```bash
minds forward
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
- **Local**: `http://127.0.0.1:8420/agents/{agent_id}/` (auto-redirects to web server)
- **Servers page**: `http://127.0.0.1:8420/agents/{agent_id}/servers/` (lists all services with local + global URLs)
- **Global** (if Cloudflare configured): `https://{service}--{agent_id}--{username}.{domain}`

## Environment variables and config

`CLOUDFLARE_FORWARDING_URL` and `SUPERTOKENS_CONNECTION_URI` come from `MindsConfig`: defaults are baked in pointing at the current dev-deployed servers, and you can override either via `~/.<MINDS_ROOT_NAME>/config.toml` (file) or environment variable (env overrides file). So no env-var setup is required for default operation.

For Cloudflare Basic-auth credentials (overriding the SuperTokens path), set these before starting the desktop client:

```bash
export CLOUDFLARE_FORWARDING_USERNAME=your-username
export CLOUDFLARE_FORWARDING_SECRET=your-secret
export OWNER_EMAIL=you@example.com
```

To pin either URL explicitly:

```bash
export CLOUDFLARE_FORWARDING_URL=https://your-modal-endpoint.modal.run
export SUPERTOKENS_CONNECTION_URI=https://your-supertokens-core.example.com
```

To run an isolated dev copy alongside an installed minds:

```bash
export MINDS_ROOT_NAME=devminds    # data lives in ~/.devminds/ with MNGR_PREFIX=devminds-
```

For agent-specific secrets (API keys, telegram credentials), set them in the template repo's `.env` file and ensure they're listed in `pass_env` in `.mngr/settings.toml`.
