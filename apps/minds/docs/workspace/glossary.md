# Glossary

Key concepts in the minds system:

- **workspace**: a persistent mngr *host*, created from a template repository via `mngr create --new-host`. All configuration lives in the template's `.mngr/settings.toml`. A workspace holds several agents: exactly one primary agent, plus the chat, worktree, and worker agents created within it over time. It is addressed by its primary agent's id, and discovered via that agent's `is_primary` label.

- **creation**: anything a user makes in their workspace. Used only at the highest conceptual level; the working vocabulary is the kinds: *apps* (opened as tabs), *skills* (an *automation* is a skill run automatically on a schedule), *data* (documents, images, notes), and *customizations* (changes to any of the above).

- **app**: something the user can open as a tab and interact with. Lives under `system/apps/<package>/` in the workspace, runs as a supervisord program, and registers its port in `data/.state/apps.toml` via `system/scripts/forward_port.py`. Each app gets a local URL (via the desktop client) and optionally a global URL (via Cloudflare tunnel). The built-in apps are the terminal, the browser, and the system interface (the special app that hosts the other tabs). Never "application" -- always "app".

- **service**: a background supervisord program with no tab (host-backup, cloudflared, the app watcher). Standalone services live under `system/services/`; a service that exists solely to support one app lives in that app's folder and is named `<app>-<role>`. "Web service" is retired vocabulary: a tab-openable thing is an app.

- **automation** [future]: a skill that runs automatically on a schedule, without the user asking. The scheduling primitive is landing separately; until then skills run when invoked.

- **customization**: a user's change to any existing part of the workspace -- a modified app, an edited skill, a tweaked chat behavior. Not a standalone kind of creation; everything in minds can be modified.

- **inspiration**: a publishable, reusable, *bootable* snapshot of the creations a mind has built, pushed to a GitHub repo so another mind can be created from it or adopt it (one repo can accumulate several inspirations). An inspiration can include zero or more creations plus customizations to existing things. See the workspace's publish-inspiration / use-inspiration skills.

- **template base**: the template state a workspace started from (or last updated itself to) -- the newest `update-self:` / `Initial workspace commit` marker on its first-parent history. Publishing an inspiration diffs against it; formerly called the "creation snapshot".

- **primary agent**: the single `system-services` agent on each workspace host, labeled `is_primary=true`. It runs bootstrap and the background services rather than a user-facing chat -- its window-0 command is `sleep infinity && claude`, so claude never starts there. Its `workspace_display_name` label holds the workspace's human-readable name (the normalized slug is the host's name). Hidden from the UI agent list and protected against direct destroy.

- **chat agent**: a user-facing mngr agent created on demand in a workspace, one per chat tab. Created with `--transfer none`, so it shares the primary agent's work_dir and Claude config dir. Bootstrap seeds the first one on initial container boot; the count grows and shrinks with the user's workload, and is not capped.

- **worktree agent**: a mngr agent created from the "New agent" tab, using `--template worktree` and `--transfer git-worktree` on branch `mngr/<name>`. Unlike a chat agent it lives in its own git worktree, outside the repo-root work_dir. Labeled `user_created=true`.

- **worker agent**: a mngr agent created by *another agent* (not by the user) when it delegates a task to a sub-agent, via the `launch-task` skill. Labeled `agent_created=true`. Not tied to any tab. The `user_created` / `agent_created` distinction drives the OOM shedding bands.

- **template repository**: a git repository (e.g. default-workspace-template) that defines a workspace's entire runtime: Dockerfile, apps, services, skills, scripts, and mngr configuration.

- **desktop client**: a local process (`minds run`) that handles authentication, agent creation, and reverse proxying. Multiplexes access to multiple workspaces through a single local endpoint.

- **bootstrap**: `uv run bootstrap`, the process that runs first-boot setup inside each agent container and then execs `supervisord -n` to launch the apps and background services.

- **supervisord**: the process-control system running inside each agent container that supervises the apps and background services, each declared as a `[program:*]` section in `supervisord.conf` (logs under `/var/log/supervisor`). Replaces the old custom service manager that watched `services.toml` and ran services in tmux windows.

- **app watcher**: a background service that monitors `data/.state/apps.toml` and writes service events to `events/services/events.jsonl` so the desktop client can discover an agent's apps. (Forwarding reconciliation happens on the minds side, via the `mngr forward` plumbing -- not in the watcher.)

- **cloudflare tunnel**: a persistent connection from the agent container to Cloudflare's network, managed by `cloudflared`. Enables global access to workspace apps protected by Cloudflare Access (Google OAuth, service tokens).

- **service event**: a JSON line in `events/services/events.jsonl` that registers (or deregisters) a name and URL for discovery. The desktop client's MngrStreamManager watches these events to discover agent backends. (The path and event vocabulary predate the app rename and are treated as plumbing.)

- **launch mode**: how the workspace runs; selects the mngr provider instance and create-template. DOCKER runs in a Docker container on the user's machine. LIMA runs in a Lima VM. VULTR runs in Docker on a Vultr VPS. AWS runs on an EC2 instance. IMBUE_CLOUD leases a pre-baked pool host via the imbue_cloud provider plugin. MODAL runs in a Modal sandbox using the local machine's own Modal token; sandboxes are ephemeral (~1 day max), so it is testing-only.
