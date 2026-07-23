# Glossary

Key concepts in the minds system:

- **workspace**: a persistent mngr *host*, created from a template repository via `mngr create --new-host`. All configuration lives in the template's `.mngr/settings.toml`. A workspace holds several agents: exactly one primary agent, plus the chat, worktree, and worker agents created within it over time. It is addressed by its primary agent's id, and discovered via that agent's `is_primary` label.

- **primary agent**: the single `system-services` agent on each workspace host, labeled `is_primary=true`. It runs bootstrap and the background services rather than a user-facing chat -- its window-0 command is `sleep infinity && claude`, so claude never starts there. Its `workspace_display_name` label holds the workspace's human-readable name (the normalized slug is the host's name). Hidden from the UI agent list and protected against direct destroy.

- **chat agent**: a user-facing mngr agent created on demand in a workspace, one per chat tab. Created with `--transfer none`, so it shares the primary agent's work_dir and Claude config dir. Bootstrap seeds the first one on initial container boot; the count grows and shrinks with the user's workload, and is not capped.

- **worktree agent**: a mngr agent created from the "New agent" tab, using `--template worktree` and `--transfer git-worktree` on branch `mngr/<name>`. Unlike a chat agent it lives in its own git worktree, outside the repo-root work_dir. Labeled `user_created=true`.

- **worker agent**: a mngr agent created by *another agent* (not by the user) when it delegates a task to a sub-agent, via the `launch-task` skill. Labeled `agent_created=true`. Not tied to any tab. The `user_created` / `agent_created` distinction drives the OOM shedding bands.

- **template repository**: a git repository (e.g. default-workspace-template) that defines a workspace's entire runtime: Dockerfile, services, skills, scripts, and mngr configuration.

- **desktop client**: a local process (`minds run`) that handles authentication, agent creation, and reverse proxying. Multiplexes access to multiple workspaces through a single local endpoint.

- **bootstrap**: `uv run bootstrap`, the process that runs first-boot setup inside each agent container and then execs `supervisord -n` to launch the background services.

- **supervisord**: the process-control system running inside each agent container that supervises the background services, each declared as a `[program:*]` section in `supervisord.conf` (logs under `/var/log/supervisor`). Replaces the old custom service manager that watched `services.toml` and ran services in tmux windows.

- **application**: a service that exposes a port for forwarding. Registered in `runtime/applications.toml` via `scripts/forward_port.py`. Each application gets both a local URL (via the desktop client) and optionally a global URL (via Cloudflare tunnel).

- **app watcher**: a background service that monitors `runtime/applications.toml`, writes service events to `events/services/events.jsonl`, and reconciles with the Cloudflare forwarding API.

- **cloudflare tunnel**: a persistent connection from the agent container to Cloudflare's network, managed by `cloudflared`. Enables global access to agent applications protected by Cloudflare Access (Google OAuth, service tokens).

- **service event**: a JSON line in `events/services/events.jsonl` that registers (or deregisters) a service name and URL. The desktop client's MngrStreamManager watches these events to discover agent backends.

- **launch mode**: how the workspace runs; selects the mngr provider instance and create-template. DOCKER runs in a Docker container on the user's machine. LIMA runs in a Lima VM. VULTR runs in Docker on a Vultr VPS. AWS runs on an EC2 instance. IMBUE_CLOUD leases a pre-baked pool host via the imbue_cloud provider plugin. MODAL runs in a Modal sandbox using the local machine's own Modal token; sandboxes are ephemeral (~1 day max), so it is testing-only.
