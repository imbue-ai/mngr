# Getting started

## Starting the forwarding server

```bash
mind forward
```

This starts the local forwarding server (default: `http://127.0.0.1:8420`). A one-time login URL is printed to the terminal.

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

1. The forwarding server clones the repo (if URL) or uses it directly (if local path)
2. Runs `mngr create` with templates from the repo's `.mngr/settings.toml`
3. If Cloudflare is configured, creates a tunnel and injects the token
4. The agent starts in a tmux session with background services

## Accessing your agent

After creation, the agent is accessible at:
- **Local**: `http://127.0.0.1:8420/agents/{agent_id}/` (auto-redirects to web server)
- **Servers page**: `http://127.0.0.1:8420/agents/{agent_id}/servers/` (lists all services with local + global URLs)
- **Global** (if Cloudflare configured): `https://{service}--{agent_id}--{username}.{domain}`

## Environment variables

For Cloudflare tunnel support, set these before starting the forwarding server:

```bash
export CLOUDFLARE_FORWARDING_URL=https://your-modal-endpoint.modal.run
export CLOUDFLARE_FORWARDING_USERNAME=your-username
export CLOUDFLARE_FORWARDING_SECRET=your-secret
export OWNER_EMAIL=you@example.com
```

For agent-specific secrets (API keys, telegram credentials), set them in the template repo's `.env` file and ensure they're listed in `pass_env` in `.mngr/settings.toml`.
