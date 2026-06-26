# Glossary

Key concepts in the minds system:

- **workspace**: a persistent mngr agent created from a template repository via `mngr create`. All configuration lives in the template's `.mngr/settings.toml`. Each workspace is labeled with `workspace=<name>` for discovery.

- **template repository**: a git repository (e.g. forever-claude-template) that defines a workspace's entire runtime: Dockerfile, services, skills, scripts, and mngr configuration.

- **desktop client**: a local process (`minds run`) that handles authentication, agent creation, and reverse proxying. Multiplexes access to multiple workspaces through a single local endpoint.

- **bootstrap**: `uv run bootstrap`, the process that runs first-boot setup inside each agent container and then execs `supervisord -n` to launch the background services.

- **supervisord**: the process-control system running inside each agent container that supervises the background services, each declared as a `[program:*]` section in `supervisord.conf` (logs under `/var/log/supervisor`). Replaces the old custom service manager that watched `services.toml` and ran services in tmux windows.

- **application**: a service that exposes a port for forwarding. Registered in `runtime/applications.toml` via `scripts/forward_port.py`. Each application gets both a local URL (via the desktop client) and optionally a global URL (via Cloudflare tunnel).

- **app watcher**: a background service that monitors `runtime/applications.toml`, writes service events to `events/services/events.jsonl`, and reconciles with the Cloudflare forwarding API.

- **cloudflare tunnel**: a persistent connection from the agent container to Cloudflare's network, managed by `cloudflared`. Enables global access to agent applications protected by Cloudflare Access (Google OAuth, service tokens).

- **service event**: a JSON line in `events/services/events.jsonl` that registers (or deregisters) a service name and URL. The desktop client's MngrStreamManager watches these events to discover agent backends.

- **launch mode**: how the agent runs. DOCKER mode runs in a Docker container on the user's machine. LIMA runs in a Lima VM. CLOUD runs in Docker on a Vultr VPS. IMBUE_CLOUD leases a pre-baked pool host via the imbue_cloud provider plugin.
