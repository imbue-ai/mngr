# Glossary

Key concepts in the minds system:

- **mind**: a persistent mngr agent created from a template repository via `mngr create`. All configuration lives in the template's `.mngr/settings.toml`. Each mind is labeled with `mind=<name>` for discovery.

- **template repository**: a git repository (e.g. forever-claude-template) that defines a mind's entire runtime: Dockerfile, services, skills, scripts, and mngr configuration.

- **forwarding server**: a local process (`mind forward`) that handles authentication, agent creation, and reverse proxying. Multiplexes access to multiple minds through a single local endpoint.

- **bootstrap service manager**: a process running inside each agent container that watches `services.toml` and starts/stops background services in tmux windows.

- **application**: a service that exposes a port for forwarding. Registered in `runtime/applications.toml` via `scripts/forward_port.py`. Each application gets both a local URL (via the forwarding server) and optionally a global URL (via Cloudflare tunnel).

- **app watcher**: a background service that monitors `runtime/applications.toml`, writes server events to `events.jsonl`, and reconciles with the Cloudflare forwarding API.

- **cloudflare tunnel**: a persistent connection from the agent container to Cloudflare's network, managed by `cloudflared`. Enables global access to agent applications protected by Cloudflare Access (Google OAuth, service tokens).

- **server event**: a JSON line in `events/servers/events.jsonl` that registers (or deregisters) a server name and URL. The forwarding server's MngrStreamManager watches these events to discover agent backends.

- **launch mode**: how the agent runs. DEV mode runs in-place on the local host. LOCAL mode runs in a Docker container. CLOUD mode is not yet implemented.
