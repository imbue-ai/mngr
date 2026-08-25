# Glossary

Key concepts in the minds system:

- **workspace**: a persistent mngr *host*, created from a template repository via `mngr create --new-host`.
  All configuration lives in the template's `.mngr/settings.toml`.
  A workspace holds several agents: exactly one primary agent, plus the chat, worktree, and worker agents created within it over time.
  It is addressed by its primary agent's id, and discovered via that agent's `is_primary` label.

- **creation**: anything a user makes in their workspace.
  Used only at the highest conceptual level; the working vocabulary is the kinds: *apps* (opened as tabs), *skills* (an *automation* is a skill run automatically on a schedule), *data* (documents, images, notes), and *customizations* (changes to any of the above).

- **app**: something the user can open as a tab and interact with.
  Lives under `system/apps/<package>/` in the workspace, runs as a supervisord program, and registers its port in `data/.state/apps.toml` via `system/scripts/forward_port.py`.
  Each app gets a local URL (via the desktop client) and, while sharing is enabled, a shared URL (via the workspace's share through the self-hosted relay).
  The built-in apps are the terminal, the browser, and the system interface (the special app that hosts the other tabs).
  Never "application" -- always "app".

- **service**: a background supervisord program with no tab (host-backup, the share-gateway, the app watcher).
  Standalone services live under `system/services/`; a service that exists solely to support one app lives in that app's folder and is named `<app>-<role>`.
  "Web service" is retired vocabulary: a tab-openable thing is an app.

- **automation** [future]: a skill that runs automatically on a schedule, without the user asking.
  The scheduling primitive is landing separately; until then skills run when invoked.

- **customization**: a user's change to any existing part of the workspace -- a modified app, an edited skill, a tweaked chat behavior.
  Not a standalone kind of creation; everything in minds can be modified.

- **template**: a publishable, reusable, *bootable* snapshot of the creations a mind has built, pushed to a GitHub repo so another mind can be created from it or adopt it (one repo can accumulate several templates).
  A template can include zero or more creations plus customizations to existing things.
  See the workspace's publish-template / use-template skills.

- **template base**: the template state a workspace started from (or last updated itself to) -- the newest `update-self:` / `Initial workspace commit` marker on its first-parent history.
  Publishing a template diffs against it; formerly called the "creation snapshot".

- **primary agent**: the single `system-services` agent on each workspace host, labeled `is_primary=true`.
  It runs bootstrap and the background services rather than a user-facing chat -- it is a plain `command`-type agent whose window-0 command is `sleep infinity`, so no claude is ever involved.
  Its `workspace_display_name` label holds the workspace's human-readable name (the normalized slug is the host's name).
  Hidden from the UI agent list and protected against direct destroy.

- **chat agent**: a user-facing mngr agent created on demand in a workspace, one per chat tab.
  Created with `--transfer none`, so it shares the primary agent's work_dir; like every claude in the workspace, it uses claude's default shared `~/.claude` config dir (`CLAUDE_CONFIG_DIR` is unset workspace-wide).
  Bootstrap seeds the first one on initial container boot; the count grows and shrinks with the user's workload, and is not capped.

- **worktree agent**: a mngr agent created from the "New agent" tab, using `--template worktree` and `--transfer git-worktree` on branch `mngr/<name>`.
  Unlike a chat agent it lives in its own git worktree, outside the repo-root work_dir.
  Labeled `user_created=true`.

- **worker agent**: a mngr agent created by *another agent* (not by the user) when it delegates a task to a sub-agent, via the `launch-task` skill.
  Labeled `agent_created=true`.
  Not tied to any tab.
  The `user_created` / `agent_created` distinction drives the OOM shedding bands.

- **template repository**: a git repository (e.g. default-workspace-template) that defines a workspace's entire runtime: Dockerfile, apps, services, skills, scripts, and mngr configuration.

- **desktop client**: a local process (`minds run`) that handles authentication, agent creation, and reverse proxying.
  Multiplexes access to multiple workspaces through a single local endpoint.

- **browser authorization component** (fully, the *desktop-app backend-server* browser authorization component): the browser-facing part of the *desktop client* -- the bare-origin web UI served by `minds run` (`apps/minds/imbue/minds/desktop_client/`) on a single local endpoint.
  It serves every page the browser reaches, carries the browser's session, and authenticates it.
  The desktop client's other duties (agent and workspace creation, reverse proxying to workspaces) sit outside the browser authorization component.

- **session**: the authenticated state of a browser connected to the *browser authorization component*, carried by the **session cookie** -- an HTTP cookie whose value is a token signed with the *installation*'s session-signing key, so a tampered cookie, one minted under another installation, or one older than 30 days is rejected.
  A browser authenticates a session by opening the one-time authentication URL that `minds run` prints to its terminal; the authenticated session is then the sole credential gating every page the component serves, and it covers all of the user's workspaces.
  It is scoped to a single *installation*, and is distinct from the optional imbue-cloud account sign-in (a separate credential for cloud-backed features).

- **installation**: one copy of the desktop client's local state -- a single data directory (e.g. `~/.minds`), so one installation = one data directory.
  Its one-time code, session-signing key, sessions, and error-reporting consent all live in that data directory and do not carry across to another one on the same machine; `minds run` pointed at a different data directory is a different installation.

- **bootstrap**: `uv run bootstrap`, the process that runs first-boot setup inside each agent container and then execs `supervisord -n` to launch the apps and background services.

- **supervisord**: the process-control system running inside each agent container that supervises the apps and background services, each declared as a `[program:*]` section in `supervisord.conf` (logs under `/var/log/supervisor`).
  Replaces the old custom service manager that watched `services.toml` and ran services in tmux windows.

- **app watcher**: a background service that monitors `data/.state/apps.toml` and writes service events to `events/services/events.jsonl` so the desktop client can discover an agent's apps.
  (Forwarding reconciliation happens on the minds side, via the `mngr forward` plumbing -- not in the watcher.)

- **share-gateway**: the background service that watches `data/.secrets/share.env` for relay materials and runs the workspace's share stack (relay tunnel + in-workspace TLS) while sharing is enabled.
  Who may access the share is controlled by the grants document (`data/.secrets/share_grants.toml`), which the desktop client rewrites as the user edits grants.

- **service event**: a JSON line in `events/services/events.jsonl` that registers (or deregisters) a name and URL for discovery.
  The desktop client's MngrStreamManager watches these events to discover agent backends.
  (The path and event vocabulary predate the app rename and are treated as plumbing.)

- **launch mode**: how the workspace runs; selects the mngr provider instance and create-template.
  DOCKER runs in a Docker container on the user's machine.
  LIMA runs in a Lima VM.
  VULTR runs in Docker on a Vultr VPS.
  AWS runs on an EC2 instance.
  IMBUE_CLOUD leases a pre-baked pool host via the imbue_cloud provider plugin.
  MODAL runs in a Modal sandbox using the local machine's own Modal token; sandboxes are ephemeral (~1 day max), so it is testing-only.

- **environment**: an environment is a single deployed instance of the minds system.
  It owns, among other things, a data root, a Modal environment, a Neon project, and a SuperTokens app.
  Every environment belongs to exactly one tier, and takes its account credentials and deploy configuration from it.
  Production and staging are environments whose names are identical to their tier names, while dev-<user> and ci-<timestamp>-<uuid> are dynamic environments that developers and CI create and destroy within their tiers.

- **tier**: a category of environment, and it determines, among other things, account credentials, deploy configuration.
  Bare metal boxes exclusively belong to one tier, and cannot be shared between them.
  Production and staging are tiers that contain exactly one environment within them, while the CI and Dev tiers may have multiple CI and Dev environments respectively.

- **adoption**: the user's own device taking ownership of a leased imbue_cloud slice's SSH trust material.
  On lease -- and on the first connect for hosts leased earlier -- the client rotates both of the slice's sshd host keys to fresh user-generated keys (pinned user-origin in mngr's host-key store, which connector bake-time material can never displace) and installs an in-VM reconciler that re-asserts the owner's `authorized_keys` and host key on every boot, after cloud-init's replay.
  After adoption, host-key trust flows only through the user's synced workspace records; the connector is trusted exactly once, at lease handoff.
  Idempotent and marker-driven; a served key that matches neither the pins nor an in-flight rotation is refused, never re-trusted.
  See `libs/mngr_imbue_cloud/README.md` ("Adoption and key rotation") and [the lost-device runbook](../deploy/lost-device-runbook.md).
