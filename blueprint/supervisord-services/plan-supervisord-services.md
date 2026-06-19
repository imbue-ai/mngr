# Supervisord-managed services

> All changes land in the `forever-claude-template` repo (worked on under `.external_worktrees/forever-claude-template`). No `mngr` / `mngr_claude` / `minds` changes are anticipated, though a few may surface during implementation.

> **Revision (post-implementation):** the "real background services agent" idea was dropped. The `system-services` agent stays on `sleep infinity` (window 0 never runs claude) -- a live window-0 agent the user could close would tear down the tmux session and supervisord with it, killing every service. The supervisord migration is unaffected; only the window-0 `command` and its `agent_args` system prompt were left as-is. The sections below are updated to match; routing error messages to a dedicated background agent can be revisited safely later.

## Overview

- Replace the hand-rolled "bootstrap service manager" (a partial supervisord reimplementation that runs each service in its own `svc-<name>` tmux window) with real **supervisord**.
- Keep the `system-services` agent's window 0 on `sleep infinity` (it never runs claude). (An earlier draft turned it into a live background Claude agent; reverted -- a window-0 agent the user could close would tear down supervisord and all services.)
- Keep the "bootstrap" entry point: `uv run bootstrap` still runs first-boot setup, then `exec`s supervisord in the foreground (consistent across docker, lima, and all VPS/cloud providers).
- Author services directly in a versioned supervisord config (delete `services.toml` entirely; no custom format or translation layer); logs are container-local and supervisord-rotated, not backed up.
- Update every skill/doc/scaffolder that encodes the old `services.toml` + `svc-` tmux-window model (notably `edit-services` and `build-web-service`).

## Expected behavior

- On boot, `uv run bootstrap` (extra_window) runs first-boot init (git config, runtime worktree, `CLAUDE_CONFIG_DIR` host-env write, backup config, initial chat agent), then hands off to supervisord which starts all services.
- Services run as supervisord programs (not tmux windows). Long-lived daemons (`system_interface`, `web`, `cloudflared`, `app-watcher`, `runtime-backup`, `host-backup`, `terminal`) restart automatically on exit; `deferred-install` runs once and does not restart.
- The `system-services` agent's window 0 stays a sleeping shell (`sleep infinity`), unchanged from before. It anchors the workspace, the host still never auto-shuts-down, and the separate first-boot chat agent still gets `/welcome`.
- Editing services means editing the supervisord config and running `supervisorctl reread && supervisorctl update` (and `supervisorctl restart <name>` for a plain bounce); supervisord no longer watches a file for changes. `supervisorctl` works even though supervisord is launched via `exec` in the foreground.
- Each service writes separate container-local stdout/stderr log files (supervisord-rotated). These are not committed to git or backed up.
- New web services created via `build-web-service` are registered as supervisord programs; the skill's verify/troubleshoot steps read supervisord logs / `supervisorctl status` instead of `tmux capture-pane -t svc-<name>`.
- `deferred-install` completion is observable via its existing per-package marker file plus `supervisorctl status` / its supervisord log (no more `svc-deferred-install` window).
- The `terminal` (ttyd) and `web` run as supervisord services; `bootstrap` is effectively the only remaining `extra_window` entry. `telegram` and `git_auth_setup` are removed; the still-needed git config commands run inside bootstrap (dropping the obsolete `gh auth setup-git`).

## Changes

### Core mechanism
- `.mngr/settings.toml`:
  - `[agent_types.main]` — keep the `command = "sleep infinity && claude"` override (window 0 never runs claude).
  - `[create_templates.main]` — trim `extra_window` to remove `telegram`, `git_auth_setup`, and `terminal` (terminal becomes a supervisord service), leaving `bootstrap`. (No `agent_args` system prompt — the live-agent idea was dropped.)
- Delete `services.toml`.
- Add a versioned supervisord config at the repo root defining every service (the 7 long-lived daemons with always-restart, `deferred-install` as a one-shot), plus the `[unix_http_server]` / `[supervisorctl]` / `[supervisord]` / `[rpcinterface]` sections; supervisord's own pidfile/socket/main-log and per-service logs live under container-local paths.
- `libs/bootstrap/` — rewrite the bootstrap program: keep all first-boot init, remove all service-management logic (reconcile, mtime watch, restart/exit detection, `svc-` window handling), add the `git config` step that replaces `git_auth_setup`, and `exec` the system `supervisord` in the foreground. Update its `README.md`, replace `manager_test.py`'s service-management tests with tests for the new setup + launch behavior, and keep the `bootstrap` / `uv run bootstrap` entry point.
- `scripts/setup_system.sh` — add `supervisor` to the shared `apt-get install` block (single install point covering the Dockerfile via `fct-setup-system` and lima/VPS providers via direct invocation).

### Services agent env + logging
- Supervisord-launched services inherit the agent environment (`MNGR_AGENT_STATE_DIR`, `CLAUDE_CONFIG_DIR`, `MNGR_HOST_DIR`, `MNGR_AGENT_ID`, `GH_TOKEN`, etc.) via the bootstrap process tree (supervisord launched from the env-sourced bootstrap shell); no per-service `environment=` enumeration.
- Each service gets separate, container-local, supervisord-rotated stdout/stderr log files.

### Skills, scaffolders, and docs
- `.agents/skills/edit-services/SKILL.md` — rewrite to describe editing the supervisord config + `supervisorctl reread/update/restart`.
- `.agents/skills/build-web-service/` — rework `scripts/scaffold_fastapi_lib.py` to append a supervisord program block (instead of a `services.toml` entry), and update `SKILL.md`, `references/cross-flow-gotchas.md`, and `references/verify.md` to use supervisord programs, `supervisorctl`, and supervisord logs (replacing `services.toml` / `svc-<name>` capture-pane usage and the port pre-flight that parses `services.toml`).
- `.agents/skills/dealing-with-the-unexpected/SKILL.md` — service status checks move from "`services.toml` vs tmux windows / bootstrap window" to `supervisorctl status` / supervisord logs.
- `.agents/skills/crystallize-task/references/post-crystallize-migration.md` — update the `services.toml` / "bootstrap manager restarts on `restart=on-failure`" guidance to supervisord semantics.
- `.agents/skills/use-ai-integration/references/billing-and-credentialing.md` — reword the "service started from `services.toml` inherits the agent's environment" note.
- `.agents/skills/update-self/SKILL.md` and `.agents/skills/submit-upstream-changes/SKILL.md` — replace `services.toml` in the shared-infra lists with the supervisord config.
- `scripts/run_ttyd.sh` — update the comment now that `terminal` runs as a supervisord service rather than an extra_window.
- `scripts/layout.py` — update the comment referencing the "bootstrap-managed forward_port.py" race (functionally unchanged; it reads `runtime/applications.toml`).
- `libs/web_server/src/web_server/runner.py` + `libs/web_server/README.md` — update docstrings/README that point at `services.toml`.
- `README.md` (repo root) and `CLAUDE.md` — rewrite the "Services" sections, the bootstrap/cwd note, and the `svc-deferred-install` capture-pane guidance (now marker file + `supervisorctl status`).

### Install / packaging
- `Dockerfile` — no separate change expected (system packages come from `fct-setup-system` → `setup_system.sh`); verify supervisord lands in the built image.
- Changelog entries for each touched project (`libs/bootstrap`, `libs/web_server`, and the root/`dev` bucket for top-level files, scripts, skills, and config).

### Not affected (verified)
- `apps/system_interface` code: the UI does not surface `svc-` tmux windows (`session_parser.py` / frontend don't read them); its "bootstrap" references (the bootstrap HTML page; the host-env file bootstrap still writes) remain valid.
- CI workflow, meta-ratchets, template-stacking test. Historical `blueprint/*` plan files are left as-is.
