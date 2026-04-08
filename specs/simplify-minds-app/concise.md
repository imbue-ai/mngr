# Simplify Minds App

## Overview

* The minds app is being simplified from a complex "mind" architecture (role agents, minds.toml, vendor repos, LLM program, conversations) to a thin wrapper around `mngr create` that launches a Claude agent from a template repo (forever-claude-template). The template's own `.mngr/settings.toml` drives all configuration.
* Port forwarding is unified under a new "applications" concept: services register their ports via `scripts/forward_port.py` into `runtime/applications.toml`, and two new in-agent services handle cloudflare tunnel management and application-to-cloudflare reconciliation. Every application gets two URLs: a local forwarding URL and a global Cloudflare URL.
* The forwarding server is kept for local proxying, authentication, and agent discovery, but its backend is heavily simplified. It also gains the ability to create Cloudflare tunnels post-agent-creation, inject tunnel tokens, and expose toggle controls for global forwarding on the per-agent servers page.
* Security is baked in by default: every new agent gets a Cloudflare tunnel with a Google OAuth access policy (gated on `OWNER_EMAIL`), and the admin credentials for the cloudflare_forwarding API never enter the agent container -- only the scoped tunnel token does.

## Expected Behavior

### Web Create Flow
* User visits the forwarding server, sees a create form with: git repo (URL or local path), agent name, launch mode (DEV/LOCAL/CLOUD)
* Submitting the form clones the repo (if URL) or uses the existing checkout (if path), then runs `mngr create` with the appropriate templates: DEV = `--template main --template dev`, LOCAL = `--template main --template docker`
* After agent creation completes, the forwarding server automatically creates a Cloudflare tunnel (via admin auth to cloudflare_forwarding API) with a default Google OAuth policy for `OWNER_EMAIL`, then injects the tunnel token into the agent via `mngr exec` (appending `export CLOUDFLARE_TUNNEL_TOKEN=...` to `runtime/secrets`)
* The agent's cloudflared service detects the new token and starts the tunnel; the app watcher reconciles applications.toml with the cloudflare API and writes server events

### Inside the Agent
* `services.toml` defines four services: claude agent (window 0), web server, ttyd, cloudflared runner, and application watcher (plus telegram bot and bootstrap from existing template)
* The web server command registers itself before starting: `python3 scripts/forward_port.py --url http://localhost:8080 --name web && python3 -m http.server 8080`
* ttyd runs via a bash wrapper that tees stderr, detects the dynamically assigned port, calls `forward_port.py`, and continues forwarding all output
* `scripts/forward_port.py` locks `runtime/applications.toml` and upserts an entry (name, url, global flag defaulting to true). Supports `--remove --name <name>` for un-registration
* The cloudflared runner service watches `runtime/secrets` via both inotify and 10-second mtime polling. When `CLOUDFLARE_TUNNEL_TOKEN` appears or changes, it starts/restarts `cloudflared tunnel run --token <token>`. All output is forwarded immediately
* The application watcher service watches `runtime/applications.toml`. On startup and on every change, it: reads the file, queries the cloudflare_forwarding API for currently registered services, diffs, adds missing services (where `global=true`), removes stale ones, and writes the full set of `server_registered` / `server_deregistered` events to `events/servers/events.jsonl`

### Per-Agent Servers Page
* The `/agents/{agent_id}/servers/` page shows each application with:
  * Local forwarding link (existing behavior, from events.jsonl)
  * Global Cloudflare link (if enabled), fetched by the forwarding server querying `GET /tunnels/{tunnel_name}/services` on the cloudflare_forwarding API
  * A toggle switch for enabling/disabling global forwarding per application
* Toggling calls a new forwarding server endpoint (`POST /agents/{agent_id}/servers/{server_name}/global`) which calls the cloudflare_forwarding API's add_service/remove_service directly -- the agent is not involved
* The `global` flag in `applications.toml` is the agent's *request*; the forwarding server / cloudflare state is authoritative for the toggle

### Server Event Handling
* The backend_resolver now handles `server_deregistered` events (new event type) in addition to `server_registered`, removing backends when a service is no longer present
* The application watcher writes ALL events (for all current applications) whenever ANY application changes, ensuring the forwarding server always has a complete picture

### Skills and Agent Knowledge
* A new skill in the template covers creating services in services.toml and registering ports via `forward_port.py`
* The skill emphasizes that agents should create proper services with wrapper scripts (not forward ports directly), so applications survive container restarts

## Changes

### Monorepo: `apps/minds/` -- Simplify Agent Creation
* Rewrite `agent_creator.py` to: accept a repo source (URL or local path), optionally clone to a temp directory, run `mngr create` with mode-appropriate templates. Remove all minds.toml loading, vendor repo logic, mngr settings configuration, and parent tracking
* Delete `vendor_mngr.py`, `mngr_settings.py`, `parent_tracking.py`, and all minds.toml-related data types (including `ClaudeMindSettings` and `claude-mind` agent type references)
* Keep launch modes (DEV/LOCAL/CLOUD) with their distinct `mngr create` invocations

### Monorepo: `apps/minds/` -- Cloudflare Tunnel Integration
* Add post-creation tunnel setup to the agent creation flow: create tunnel via cloudflare_forwarding API (admin auth), set default auth policy (Google OAuth for `OWNER_EMAIL`), inject tunnel token into agent via `mngr exec`
* Add environment variables to the forwarding server: `CLOUDFLARE_FORWARDING_URL`, `CLOUDFLARE_FORWARDING_USERNAME`, `CLOUDFLARE_FORWARDING_SECRET`, `OWNER_EMAIL`
* Tunnel name format: `{CLOUDFLARE_FORWARDING_USERNAME}--{agent_id}`

### Monorepo: `apps/minds/` -- Forwarding Server UI and API
* Update the web create form: add agent name field, launch mode dropdown (DEV/LOCAL/CLOUD), support local path in addition to URL for the repo field
* Update the per-agent servers page template to show both local and global URLs per application, plus a toggle switch for global forwarding
* Add `POST /agents/{agent_id}/servers/{server_name}/global` endpoint that calls cloudflare_forwarding API add_service/remove_service
* Add logic to query cloudflare_forwarding API for current global services when rendering the servers page

### Monorepo: `apps/minds/` -- Backend Resolver
* Add handling for `server_deregistered` event type: remove the backend from the resolver's server map when this event is received

### Template Repo: `forever-claude-template` -- New Scripts
* Add `scripts/forward_port.py`: file-locked upsert/remove for `runtime/applications.toml`. Accepts `--url`, `--name`, `--global` (default true), and `--remove --name` for deletion

### Template Repo: `forever-claude-template` -- New Services
* Add `libs/cloudflare_tunnel/`: service that watches `runtime/secrets` (inotify + 10s polling fallback) for `CLOUDFLARE_TUNNEL_TOKEN`, starts/restarts `cloudflared tunnel run --token <token>`, forwards all output immediately
* Add `libs/app_watcher/`: service that watches `runtime/applications.toml`, reconciles with cloudflare_forwarding API on startup and on every change (add missing, remove stale), writes full set of `server_registered`/`server_deregistered` events to `events/servers/events.jsonl`
* Add ttyd as a service in `services.toml`: bash wrapper that runs ttyd with `-p 0`, tees stderr to detect the port, calls `forward_port.py`, and continues forwarding all output
* Update `services.toml` to include: web server (with forward_port.py call), ttyd, cloudflared runner, application watcher

### Template Repo: `forever-claude-template` -- Configuration
* Add `CLOUDFLARE_FORWARDING_URL` to `pass_env` in `.mngr/settings.toml`
* Rename the "local" template to "dev" in `.mngr/settings.toml`

### Template Repo: `forever-claude-template` -- Skills
* Add a skill for creating services and forwarding ports: covers editing services.toml, writing wrapper scripts, and calling `forward_port.py`. Emphasizes creating proper services over direct port forwarding

### Monorepo: `apps/minds/` -- Code Removal
* Remove all minds.toml-related data types and parsing
* Remove `claude-mind` agent type and all references
* Remove vendor_mngr.py (git subtree vendoring)
* Remove mngr_settings.py (mngr settings.toml generation for minds)
* Remove parent_tracking.py (.parent file management)
* Remove/simplify event lifecycle, conversation, and watcher infrastructure that is no longer needed
* Keep the electron desktop app as-is
