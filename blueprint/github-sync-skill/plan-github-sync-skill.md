# Plan: github-sync skill (replace the always-on runtime-backup service)

## Overview

- Remove the always-on `runtime-backup` service and all of its supporting machinery from default-workspace-template. GitHub syncing becomes strictly opt-in: a new `github-sync` skill sets everything up only when the user asks for it.
- Rename the concept from "backup" to "github sync". The real backups are the restic-based `host-backup` service (which already covers all of `/mngr/`, including `runtime/`), so the old name was confusing and the git-based mechanism is better described as syncing.
- When enabled, the skill provides three things: a dedicated private GitHub repo wired up as `origin`, a periodic service that commits and pushes `runtime/`, and a post-commit hook so every commit in `/mngr/code` and in worker worktrees auto-pushes its current branch.
- All GitHub access flows through latchkey (repo creation via the GitHub API, git pushes via the latchkey git gateway, with the per-VPS secondary gateway as the backup path on remote hosts). `GH_TOKEN` is removed from the template entirely -- no credential ever enters the container.
- Private repositories are mandatory: the skill refuses public repos at setup, and the sync service periodically re-verifies visibility and halts pushes with a surfaced warning if the repo is public or cannot be confirmed private (agents might otherwise push secrets without thinking about it).
- The sync runner code stays in the template as a dormant library (`libs/runtime_backup` renamed to `libs/github_sync` and extended), so it is exercised by CI; the skill only performs setup and adds the supervisord `[program]` block.

## Expected behavior

### Default (sync not enabled)

- A fresh workspace has none of the machinery: no `runtime/` git worktree, no orphan branch, no restore-on-recreate, no auto-push hook, no `core.hooksPath` install, and no mention of `GH_TOKEN` anywhere. `runtime/` is a plain gitignored directory.
- The restic `host-backup` service is unchanged and remains the durability story for un-synced workspaces.

### Enabling sync

- The user asks for github sync (purely on-demand; the skill is discoverable like any other skill).
- The skill sends one combined latchkey permission request covering GitHub repo creation (API) and `github-git-write` (pushes), then waits for approval in the minds app.
- The skill creates a brand-new private GitHub repo:
  - Default name: the workspace name as-is (numeric suffix on collision), confirmed with the user before creation.
  - Default owner: the authenticated user's personal account; the user can name an org/owner at the confirmation step.
- If `origin` already points at a never-synced, user-owned private repo, the skill asks whether to reuse it or create a fresh one, recommending a new repo unless the user has a particular reason not to. Reuse still requires verifying the repo is private and writable.
- `origin` is repointed at the sync repo -- `origin` is reserved for the sync remote (upstream-template flows keep using `parent.toml` and are unaffected).
- The skill persists the latchkey gateway wiring in git config so a plain `git push` works transparently for every agent and worker, preferring the primary gateway and falling back to the per-VPS secondary gateway when the user's machine is offline.
- Initial sync pushes: the current main branch (full history), a new stable orphan branch `runtime-sync` holding `runtime/` as a worktree, and any existing worker branches.
- The skill adds a `[program:github-sync]` block to `supervisord.conf` and starts it via `supervisorctl` (edit-services pattern).
- The skill installs the post-commit hook (via `core.hooksPath`) so every checkout in the container auto-pushes its branch after each commit, in the background, never blocking the commit. The hook skips `runtime-sync` (the service owns it) and silently no-ops when sync is not configured.
- The enablement's own changes (supervisord block, git wiring, skill state) are committed and pushed as normal commits, making sync sticky across recreations from the repo.

### Ongoing behavior

- Every ~60s the service commits and pushes `runtime/` to `runtime-sync` (no-op when clean). `runtime/secrets` is excluded via the worktree's own `.gitignore` and never reaches the remote.
- The service auto-commits only `runtime/`. Code and worker branches are pushed only when agents actually commit (via the hook); uncommitted working-tree changes are never captured.
- Push failures (e.g. gateway unreachable) are logged and retried on the next tick or next commit. Commits made while offline are pushed the next time a commit lands on that branch; `runtime-sync` self-heals on the next tick.
- The service periodically re-verifies the repo is still private; if it is public or cannot be confirmed, pushes halt and a warning is surfaced until resolved.

### Recreating a workspace from a synced repo

- A workspace created from the private sync repo inherits the committed service config, but not the latchkey grants (fresh host = fresh permissions) or the `runtime/` worktree.
- Self-healing: the synced-in service detects the missing permissions/worktree on startup, and the skill guides re-authorization; once granted, `runtime/` is restored from the `runtime-sync` branch automatically, bringing back memory, tickets, and transcripts.

### Disabling sync

- The skill handles disable: stop and remove the `[program:github-sync]` block, remove the hook wiring and the gateway git config -- a full unwind of the container-side setup.
- The local `runtime/` worktree and its history are kept intact (harmless, preserves state, easy re-enable).
- The skill prompts the user whether to keep or delete the remote repo on GitHub.

### Status

- The skill also answers "what's my sync status?": service state, last successful push, last visibility check, and the repo URL.

### Legacy workspaces

- No migration tooling. Existing workspaces running `runtime-backup` with `mindsbackup/$MNGR_AGENT_ID` branches keep working until they update-self; after updating, `runtime/` simply stops being git-synced until the user opts into github sync. This is documented in the changelog and the skill.

## Changes

All functional changes land in the default-workspace-template repo (worked on via a worktree under `.external_worktrees/` per monorepo convention); the mngr monorepo gets only doc/changelog touches.

- `supervisord.conf`: remove the `[program:runtime-backup]` block (the skill adds `[program:github-sync]` on enable; a commented example or skill-owned snippet documents the exact block).
- `libs/runtime_backup` renamed to `libs/github_sync`, extended to own everything syncing needs: the periodic commit+push runner (latchkey-gateway pushes, stable `runtime-sync` branch), the periodic repo-visibility check with push-halt, worktree/orphan-branch setup, restore-from-remote, and status reporting used by the skill.
- `libs/bootstrap`: remove the `runtime/` worktree init, `mindsbackup` restore-on-recreate, `core.hooksPath` install, and the `GH_TOKEN`-gated initial push. Setup moves into `libs/github_sync`, invoked by the skill. (The git `https://` remote rewrites need a decision: keep only if still needed for latchkey-gateway pushes.)
- `scripts/git_hooks/post-commit`: rewritten with no `GH_TOKEN` gating -- it pushes the current branch via the persisted origin/gateway wiring, skips `runtime-sync`, and no-ops when sync is not configured. No longer installed by default; the skill installs it.
- `.mngr/settings.toml`: drop `GH_TOKEN` from `pass_env`; remove every other `GH_TOKEN` usage/mention across the template (bootstrap comments, edit-services skill text, README, CLAUDE.md, docs).
- New `.claude/skills/github-sync/` skill covering: enable (permission request, repo create/reuse with name+owner confirmation, origin repoint, gateway git wiring, initial pushes, supervisord block + `supervisorctl`), disable (full unwind + keep/delete-repo prompt), status, and the self-heal/restore flow for recreated workspaces.
- Template docs updated (README service list, edit-services and latchkey skill cross-references, any doc describing the runtime backup story).
- Tests: unit/integration tests for the `libs/github_sync` logic against a local git remote standing in for GitHub (commit loop, orphan-branch setup and restore, visibility-halt behavior, push-retry paths); manual end-to-end verification of the skill flow against real latchkey + GitHub.
- Monorepo: changelog entries and updates to any minds docs that mention the runtime-backup service; historical specs stay as-is.
