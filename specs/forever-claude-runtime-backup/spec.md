# Forever-claude-template runtime git backup

## Overview

- **Goal:** Never lose important agent state from a minds workspace by continuously backing up the gitignored `runtime/` folder (which will now also contain `memory/`) to the same per-workspace private git repo on a separate orphan branch.
- **Why:** Today, container loss wipes all transcripts, Claude memory, ticket state, telegram history, etc. With this change, "migrate to a totally new workspace" becomes "clone the private repo, set `GH_TOKEN`, start a fresh container."
- **Approach:** Make `runtime/` a git worktree of an orphan branch (`mindsbackup/$MNGR_AGENT_ID`) on the same `origin` as the main checkout. A 60-second polling service commits + pushes any changes. A `post-commit` hook auto-pushes the *main* repo too, both gated on `GH_TOKEN` being set.
- **Constraints:** Stupid and simple. One writer per backup branch (no force-push). `runtime/secrets` is gitignored inside the backup branch (the real Cloudflare token must never be pushed). Workers don't run the backup service (only the outer main agent does), but they DO inherit `GH_TOKEN` so their own `post-commit` hook can auto-push their working branch.

## Expected Behavior

### From the user's perspective

- User generates a `GH_TOKEN` scoped to one private fork of forever-claude-template, then sets it in the env that runs `mngr create`.
- Every ~60s after the workspace is up, the contents of `runtime/` (including `runtime/memory/` for Claude's auto-memory and any task / transcript artifacts under `runtime/<skill>/<slug>/`) are visible on the `mindsbackup/$MNGR_AGENT_ID` branch of their private repo.
- Whenever the agent commits to the main repo, the post-commit hook also pushes the active branch to origin in the background.
- If the user runs `mngr create` *without* `GH_TOKEN`, the workspace runs normally and `runtime/` is still committed locally to the orphan branch, but nothing is pushed. To start pushing later the user must supply `GH_TOKEN` and recreate the container: bootstrap's fresh-create path then re-runs and pushes with `--set-upstream`, after which subsequent backup ticks keep the branch up to date. Local-only commits made in the previous (offline) container are lost when that container is destroyed -- offline-only mode does not preserve history across container loss.
- If the same `MNGR_AGENT_ID` is recreated on a fresh container, the existing `mindsbackup/$MNGR_AGENT_ID` branch is fetched and materialized into `runtime/` on first boot (restore). Cross-agent migration is intentionally manual; no tooling.

### From the system's perspective

- Bootstrap (already the first window started in the main template) gains a one-time pre-services init step that ensures `runtime/` exists as a worktree of `mindsbackup/$MNGR_AGENT_ID` *before* any service that writes into `runtime/` (cloudflared, app-watcher, telegram) starts.
- A new service `runtime-backup` is added to `services.toml`. Each tick: `git add`, `git commit` (skipped if no changes), `git push`. No exotic logic, no inotify, no diff stat.
- The post-commit hook is installed via `core.hooksPath = /code/scripts/git_hooks` so it applies to every checkout (main, worker sub-agents, runtime worktree). The hook self-skips when:
  - `GH_TOKEN` is unset, OR
  - the current branch starts with `mindsbackup/` (the polling service handles those pushes).
- Worker sub-agents *do* receive `GH_TOKEN` (it's in `[commands.create].pass_env` and worker templates do not override that), so their post-commit hook auto-pushes their working branch. Workers do not run the runtime-backup service; only the outer main agent does.
- Bootstrap init failures (network blip, missing token) are logged loudly to stderr but do not block service startup. The runtime-backup service retries on its own tick.
- `memory/` is moved to `runtime/memory/` by updating `.claude/settings.json` (`autoMemoryDirectory`), `.gitignore`, and `CLAUDE.md`. Existing populated `memory/` dirs at the repo root are NOT auto-migrated (fresh installs only).
- `tk` ticket storage is moved from `.tickets/` to `runtime/tickets/` via `TICKETS_DIR=/code/runtime/tickets` in `host_env` so tickets are also covered by the backup branch. Existing `.tickets/` dirs are not auto-migrated.
- Force-push is never used; per-agent branches mean exactly one writer.

### Interactions worth calling out

- Bootstrap's init runs once at startup, before reconciling `services.toml`. Subsequent bootstrap loop iterations (which exist to detect `services.toml` edits) do not re-run init.
- The runtime-backup service can assume the worktree exists. If it ever doesn't (someone deleted it manually), the next commit attempt errors, is logged, retried, no special recovery.
- `runtime/secrets` is gitignored inside the runtime worktree's own `.gitignore` (a separate file from the main repo's `.gitignore`, which already excludes the whole `runtime/`).
- The same `runtime/secrets` file continues to be the channel for `CLOUDFLARE_TUNNEL_TOKEN`. Nothing about the cloudflared service changes.
- `GH_TOKEN` is **not** delivered through `runtime/secrets`; it arrives via env (`pass_env`), matching the existing `~/project/mngr/.mngr/settings.toml` pattern.

## Implementation Plan

All paths below are relative to the forever-claude-template repo root unless noted otherwise.

### 1. Move `memory/` and `tk` tickets into `runtime/`

- `.claude/settings.json`: change `"autoMemoryDirectory": "memory"` → `"autoMemoryDirectory": "runtime/memory"`.
- `.gitignore`: remove the standalone `memory/` line (the whole `runtime/` is already gitignored, so `runtime/memory/` is covered transitively). `.tickets/` stays gitignored as a safety net for any code path that bypasses `TICKETS_DIR`.
- `CLAUDE.md` Memory section: update the path and note that memory is now backed up via the `mindsbackup/$MNGR_AGENT_ID` branch.
- `.mngr/settings.toml`'s `[commands.create].host_env`: add `TICKETS_DIR=/code/runtime/tickets` so tk stores tickets inside the backup worktree.
- `CLAUDE.md` ticket-system bullet and `README.md`: update the documented path from `.tickets/` to `runtime/tickets/`.

### 2. Runtime worktree + initial branch state

- Branch name: `mindsbackup/${MNGR_AGENT_ID}`. `MNGR_AGENT_ID` is read from env (mngr sets it for every agent it manages).
- Inside the runtime worktree:
  - `.gitignore` containing `secrets` (excludes the secrets file from being committed).
  - An initial empty commit (only on first creation) so the orphan branch has a parent and `git push` works without `--force`.
- Bot identity for runtime commits: `user.name=runtime-backup`, `user.email=runtime-backup@mindsbackup.local`. Configured via `git -C runtime/ config user.name/email` so the main checkout's identity is unchanged.
- Commit message format: `runtime backup: <ISO 8601 UTC timestamp>` (e.g. `runtime backup: 2026-05-06T17:42:13Z`). No diff stat, no body.

### 3. New service runner — `libs/runtime_backup/`

New package matching the layout of `libs/cloudflare_tunnel/`:

```
libs/runtime_backup/
  pyproject.toml
  README.md
  test_runtime_backup_ratchets.py
  src/runtime_backup/
    __init__.py
    runner.py
    runner_test.py
```

- `pyproject.toml`: declares one console script `runtime-backup = "runtime_backup.runner:main"` and one runtime dep on `loguru` (matching the other service packages).
- `runner.py:main()`: structured as a one-time startup phase followed by an infinite tick loop.
  - Startup (runs exactly once before entering the loop):
    - If `GH_TOKEN` is unset, log `[runtime-backup] no GH_TOKEN, skipping push` once. (Per-tick logging is intentionally avoided to prevent log spam.)
  - Tick loop: sleep 60s, then run one tick. Tick logic:
    1. `git -C runtime/ add -A`.
    2. If `git -C runtime/ status --porcelain` is empty, skip commit + push (nothing changed) and continue to the next tick.
    3. Else `git -C runtime/ commit -m "runtime backup: <ISO timestamp>"`.
    4. If `GH_TOKEN` is set, attempt `git -C runtime/ push` (this also covers cases where a prior tick committed but failed to push). On failure, fall back to `git -C runtime/ push --set-upstream origin {branch}` so the service self-heals when bootstrap's initial `--set-upstream` push didn't succeed (bootstrap's push is best-effort per §4 step 6, so upstream may not yet be configured). No `--force`. If `GH_TOKEN` is unset, skip the push entirely.
    5. On any subprocess error other than "nothing to commit": log to stderr and append to `/tmp/runtime-backup.log`, continue to next tick. Never exit.
- `README.md`: describes the service contract, log path, branch naming convention.
- `test_runtime_backup_ratchets.py`: import-only ratchets matching the other services.

### 4. Bootstrap pre-services init — `libs/bootstrap/src/bootstrap/manager.py`

Add a `_init_runtime_worktree()` function called once from `main()` *before* the first `_reconcile()` call. Behavior:

1. Read `MNGR_AGENT_ID` from env. If unset, log a warning and return (bootstrap continues; runtime-backup service will also no-op).
2. Compute `branch = f"mindsbackup/{MNGR_AGENT_ID}"`.
3. If `runtime/.git` already exists (worktree already set up from a prior bootstrap run on the same container), return early.
4. Probe origin to decide between restore and fresh-create. The decision must distinguish three cases — restore, fresh-create, and "can't tell yet" — because silently falling back to fresh-create when origin actually has the branch would create a divergent local orphan that can never push (force-push is forbidden) and would violate the one-writer-per-branch invariant.
   - If `GH_TOKEN` is unset, treat as "fresh-create" (offline-only mode; nothing to fetch). Note: if `GH_TOKEN` is later supplied for the same `MNGR_AGENT_ID` and the remote branch already exists, the next push will fail; that is an explicit limitation of offline-only mode.
   - Else run `git ls-remote --exit-code origin "refs/heads/{branch}"`:
     - Exit 0 with output → branch exists on origin → fetch it (`git fetch origin {branch}`) and proceed to step 5 (restore).
     - Exit 2 (no matching ref) → branch does not exist on origin → proceed to step 6 (fresh-create).
     - Any other failure (network down, auth error, etc.) → log loudly to stderr and SKIP the rest of init. Do NOT create an orphan locally. The runtime-backup service and the next bootstrap restart will retry; this avoids producing a divergent local history for a branch that may already exist on origin.
5. Restore path:
   - `git worktree add -b {branch} runtime/ origin/{branch}` so the worktree is on a local branch by that name (a bare `git worktree add runtime/ origin/{branch}` would leave the worktree in detached HEAD, which would later break `git push` from the runtime-backup service).
   - Configure the local branch to track `origin/{branch}` (e.g. `git -C runtime/ branch --set-upstream-to=origin/{branch}`).
   - Restore complete; return.
6. Fresh-create path (origin confirmed not to have the branch, or offline-only mode):
   - Race-avoidance preamble: if `runtime/` already exists with files (e.g. cloudflared got there first), rename it to `runtime.preexisting/` so the next step has a clean target. After the worktree-add below succeeds, move the files from `runtime.preexisting/` back into `runtime/` and `rmdir runtime.preexisting/`. If `runtime/` does not pre-exist, skip this preamble entirely.
   - `git worktree add --orphan -b {branch} runtime/` (the single worktree-creation command for this path; runs whether or not the preamble fired).
   - Inside `runtime/`: write `.gitignore` containing `secrets`, set bot identity (§2), `git -C runtime/ commit --allow-empty -m "runtime backup: <ISO 8601 UTC timestamp>"` (same format as every other commit per §2; the timestamp is the init time).
   - If `GH_TOKEN` is set: `git -C runtime/ push --set-upstream origin {branch}` (best-effort; failure is logged but non-fatal).
7. All errors are logged to stderr; bootstrap proceeds to reconcile services either way.

### 5. Post-commit hook — `scripts/git_hooks/post-commit`

A bash script. Logical flow:

```
1. [ -z "$GH_TOKEN" ] && exit 0
2. branch="$(git symbolic-ref --short HEAD 2>/dev/null)" || exit 0   # detached: skip
3. case "$branch" in mindsbackup/*) exit 0 ;; esac                    # runtime worktree: skip
4. {
       git push 2>&1 \
         || git push --set-upstream origin "$branch" 2>&1
   } >> /tmp/post-commit-push.log &
   disown 2>/dev/null || true
5. exit 0
```

- Background push is detached so the hook returns instantly; the commit is never blocked.
- Both stdout and stderr go to `/tmp/post-commit-push.log` (append-only, no rotation; container restart wipes it).
- Marked executable (`chmod +x`) and committed to git.

### 6. Wire-up — `.mngr/settings.toml` and `services.toml`

- `.mngr/settings.toml`:
  - `[commands.create].pass_env`: append `"GH_TOKEN"` to the existing list.
  - `[create_templates.main].extra_window` `git_auth_setup` entry: append `&& git config --global core.hooksPath /code/scripts/git_hooks` to the existing chain (it already runs `git config --global url… && gh auth setup-git`).
  - Worker templates (`[create_templates.worker]` and `[create_templates.crystallize-worker]`) intentionally do NOT override `pass_env`, so they inherit `GH_TOKEN` from `[commands.create].pass_env` and their post-commit hook auto-pushes the worker's branch. (Workers still don't run the runtime-backup service because they don't include the `bootstrap` extra_window.)
- `services.toml`: add `[services.runtime-backup]` with `command = "uv run runtime-backup"` and `restart = "on-failure"`.
- Root `pyproject.toml`: add `libs/runtime_backup` to the workspace members so `uv sync --all-packages` picks it up.
- `Dockerfile`: no change required — `uv sync --all-packages` already runs at image build and will install the new package.

### 7. Documentation updates

- `CLAUDE.md` Git section: replace `Commit your changes locally. … Do not push to remote.` with: `Commit your changes locally. The post-commit hook auto-pushes when GH_TOKEN is set; you don't need to push manually. runtime/ (including runtime/memory/) is backed up automatically on the mindsbackup/$MNGR_AGENT_ID branch by the runtime-backup service.`
- `CLAUDE.md` Memory section: update path (see §1).
- New `libs/runtime_backup/README.md`: describes service contract.
- `.agents/skills/edit-services/SKILL.md`: if it lists current services, mention `runtime-backup`. (Verify whether it actually enumerates services.)

## Implementation Phases

Each phase ends with a working, observably better system.

### Phase 1 — Memory + tickets move

- Update `.claude/settings.json`, `.gitignore`, `CLAUDE.md` (memory section and ticket-system bullet), `README.md` (ticket path), and `.mngr/settings.toml`'s `[commands.create].host_env` (add `TICKETS_DIR=/code/runtime/tickets`).
- Verify on a fresh container: `runtime/memory/` is created on first auto-memory write, and `tk` writes new tickets under `runtime/tickets/`; nothing else changes.
- Smallest possible change; isolates the directory moves from anything git-related.

### Phase 2 — Runtime worktree + backup service (local-only)

- Add `libs/runtime_backup` package and `[services.runtime-backup]` entry.
- Add bootstrap init step (§4) but skip the push branch entirely — just create the orphan branch + worktree locally; the service commits locally on each tick.
- Verify in a fresh container: `git -C runtime/ log --oneline` shows ticking commits; main repo's `git status` is unaffected; `runtime/secrets` written by cloudflared is *not* tracked.

### Phase 3 — `GH_TOKEN` plumbing + push

- Add `GH_TOKEN` to `[commands.create].pass_env`. Append `core.hooksPath` config to `git_auth_setup` window.
- Bootstrap init: push the initial commit if `GH_TOKEN` is set.
- Runtime-backup service: push every tick if `GH_TOKEN` is set.
- Add the post-commit hook script at `scripts/git_hooks/post-commit`.
- Verify: with `GH_TOKEN` set, `mindsbackup/$MNGR_AGENT_ID` appears on origin and grows over time; without `GH_TOKEN`, container behaves as Phase 2.

### Phase 4 — Worker behavior + restore-on-restart

- Verify the resolved worker semantics (§6): worker containers inherit `GH_TOKEN` from `[commands.create].pass_env`, so their `post-commit` hook auto-pushes their working branch; they do NOT run the runtime-backup service because their template does not include the `bootstrap` extra_window.
- Recreate the same `MNGR_AGENT_ID` after destroying the container; verify bootstrap restores `runtime/` from the existing remote branch and that `runtime/memory/` content reappears.

### Phase 5 — Docs + polish

- Update `CLAUDE.md`, `edit-services` skill, write `libs/runtime_backup/README.md`.
- Add ratchet tests; ensure full test suite is green.

## Testing Strategy

### Unit tests

- `libs/runtime_backup/src/runtime_backup/runner_test.py`:
  - One tick with no changes → no commit, no push.
  - One tick with changes → commit happens with the expected message format.
  - With `GH_TOKEN` unset → push is not invoked.
  - Subprocess failure on push → caught, logged, loop continues.
- `libs/bootstrap/src/bootstrap/manager_test.py` (new tests for the init function):
  - Branch already exists on origin → fetch + worktree-add path taken.
  - Branch doesn't exist → orphan-create path; `.gitignore` contains `secrets`; initial commit is made.
  - `MNGR_AGENT_ID` unset → init function returns early without raising.
  - `runtime/` already populated → files are preserved into the new worktree.
- Reuse fixtures from existing `libs/bootstrap` and `libs/cloudflare_tunnel` test setups (especially anything that mocks `subprocess.run`).

### Integration tests (`test_*.py`)

- A test that boots bootstrap against a temp git repo (a local bare repo serves as `origin`) and asserts:
  - `runtime/` becomes a worktree of `mindsbackup/<id>`.
  - The runtime-backup service can complete one full tick that ends with `origin/mindsbackup/<id>` advancing.
  - Both with and without `GH_TOKEN` set.
- A test exercising the post-commit hook script:
  - Exits 0 with no token set.
  - Exits 0 with branch `mindsbackup/foo`.
  - Pushes when both conditions are healthy.

### Edge cases worth covering

- `runtime/` exists with files when bootstrap runs (cloudflared got there first under a misordered start).
- Network down during init: bootstrap proceeds; service retries on next tick.
- Two consecutive ticks where nothing changed: only one commit total.
- A worktree that lost its ref (e.g. main checkout's `.git/worktrees/` got stale): runtime-backup logs and continues; not a blocker.
- (`MNGR_AGENT_ID` is generated from a ref-safe alphabet by mngr, so no sanitization is needed.)

### Manual verification before declaring done

- Spin up a real minds workspace with a real `GH_TOKEN`. Trigger a Claude memory write. After 60s, observe a commit on `mindsbackup/$MNGR_AGENT_ID` on GitHub. Tail `/tmp/post-commit-push.log` after a normal `git commit` on main to confirm the hook fires.
- Spin up the same workspace name a second time after destroying the first. Verify `runtime/memory/` and other prior content reappears in the new container.

## Open Questions

1. **Bootstrap behavior when init fails *and* `GH_TOKEN` is unset.** Decision per Q&A: still start the runtime-backup service (it no-ops cleanly without push). Spec assumes this; flagging in case the implementer wants to revisit.
2. **`/tmp/` log persistence.** `/tmp/` is wiped on container restart. If you want the log to survive restarts, the spec would need to point logs into `runtime/` (and gitignore them). Current decision per Q&A is `/tmp/`.
3. **Push backoff.** Current design retries every tick (60s) on push failure with no backoff. If origin is hard-down for hours, that's ~60 retries an hour. Probably fine for "stupid and simple"; flagging in case.
4. **Multi-agent isolation in a shared private fork.** If a user reuses one fork for several agents, branches accumulate (`mindsbackup/agent-A`, `mindsbackup/agent-B`, …). No automatic cleanup; user prunes manually.
5. **Pre-existing memory / tickets migration.** §1 deliberately doesn't migrate existing populated `memory/` or `.tickets/` dirs at the repo root — fresh installs only per the Q&A. If you change your mind, add a one-shot move in bootstrap that runs before the new `autoMemoryDirectory` / `TICKETS_DIR` settings take effect.

Resolved during Q&A and removed: worker `pass_env` semantics (workers do receive `GH_TOKEN` and auto-push their branches; that's intentional), and `MNGR_AGENT_ID` ref-safety (the id is always ref-safe by construction).
