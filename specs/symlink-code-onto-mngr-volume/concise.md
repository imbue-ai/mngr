# Symlink /code/ onto the /mngr/ volume in forever-claude-template

## Overview

- Move the FCT workspace from `/code/` to `/mngr/code/` so it shares the persistent volume that already backs the rest of `/mngr/` (mngr's `host_dir`).
- Motivation: a single snapshot of the `/mngr/` volume now captures *everything* — agent state, workspace code, worktrees, runtime/, tickets, etc. — across docker, lima, and remote VPS providers.
- `/code -> /mngr/code` and `/worktree -> /mngr/worktree` are kept as safety-net symlinks only; every FCT-owned reference is rewritten to the real `/mngr/...` path. Anything still hitting `/code/...` resolves transparently through the symlink.
- Docker-image-based providers (`docker`, `vultr`, `ovh`) handle the build-time-vs-runtime mismatch by baking the workspace at `/mngr/code/`, then renaming it to `/docker_build_code` at the end of the build so the volume mount path is empty in the shipped image; an inlined CMD-side seed step atomically relocates it onto the volume on first boot.
- `lima` aligns on `target_path = /mngr/code/` too, but skips the docker-build dance because its `/mngr/` volume is host-bind-mounted before the VM starts; `imbue_cloud` inherits via the `ovh` bake and needs no separate change.

## Expected Behavior

- Every newly-created FCT agent (on docker / vultr / ovh / lima) has `work_dir = /mngr/code/`; users SSH-ing in or running `mngr connect` land at `/mngr/code/` and shell prompts reflect that path.
- `/code` exists as a symlink to `/mngr/code/`, and `/worktree` as a symlink to `/mngr/worktree/`, on every provider — so any stray `cd /code`, hard-coded `/code/...` script, or muscle-memory path still works without modification.
- On first boot of a fresh container/VM volume, the workspace is seeded onto the volume by an atomic two-step move:
  - `/docker_build_code` is copied to `/mngr/code.moving` (cross-filesystem copy that lands on the volume).
  - On success, `/mngr/code.moving` is atomically renamed to `/mngr/code`.
  - `/docker_build_code` is then cleaned up.
- If `/mngr/code.moving` already exists from a previously-interrupted seed, it is wiped first and the seed restarts from `/docker_build_code`.
- If `/mngr/code` already exists on the volume, the seed step is a no-op — the volume's content is canonical, and `/docker_build_code` is left alone (or cleaned up) without overwriting the workspace.
- If the seed sources are all missing (no `/docker_build_code`, no `/mngr/code`, no `/mngr/code.moving`), the container logs a loud error and exits nonzero so the failure surfaces in mngr/docker rather than silently sleeping forever.
- `/mngr/worktree/` is `mkdir -p`'d on every boot so the `/worktree` symlink always resolves, regardless of whether worktrees have been created yet.
- The existing PID-1 `trap 'exit 0' TERM; tail -f /dev/null & wait` signal behavior is preserved — seeding is inlined into the same `CMD`, not bolted on via a separate `ENTRYPOINT`.
- Image upgrades only refresh system-level deps (apt packages, claude binary, uv, latchkey, node, etc.); the workspace at `/mngr/code/` is owned by the volume once seeded and is only updated via `parent.toml` self-update, `mngr push`, or in-agent edits. This is documented as the intended semantics.
- Existing live hosts running the *old* image keep working unchanged — they don't auto-migrate to the new layout. Anyone who wants to move an existing host onto the new layout must do so manually; the spec calls this out as a known limitation.
- All affected tests (e.g. `test_mngr_template_stacking.py`'s `target_path == "/code/"` assertion, deployment/e2e tests that reference the FCT workspace path) pass against the new `/mngr/code/...` paths.

## Changes

- `.mngr/settings.toml`:
  - `commands.create.host_env__extend`: rewrite `TICKETS_DIR=/code/runtime/tickets` to `TICKETS_DIR=/mngr/code/runtime/tickets`, and rewrite any other `/code/...` literal in that block.
  - `commands.create.worktree_base_folder`: change `/worktree/` to `/mngr/worktree/`.
  - `create_templates.docker`, `create_templates.vultr`, `create_templates.ovh`: change `target_path = "/code/"` to `target_path = "/mngr/code/"`.
  - `create_templates.lima`: change `target_path = "/code/"` to `target_path = "/mngr/code/"`, and add commands to `extra_provision_command__extend` that create the safety-net symlinks (`/code -> /mngr/code`, `/worktree -> /mngr/worktree`) and ensure `/mngr/worktree/` exists with the right ownership. Adjust the existing `mkdir/chown /worktree` + `cd apps/system_interface/frontend ...` + `uv tool install vendor/mngr/...` etc. lines so any `/code/...` path becomes `/mngr/code/...`.
  - `create_templates.imbue_cloud`: no path changes (inherits via the `ovh` bake).
  - Any other template-level `/code/...` reference in this file (comments included) is rewritten to `/mngr/code/...`.

- `Dockerfile`:
  - Change `WORKDIR /code/` to `WORKDIR /mngr/code/`.
  - Rewrite every `COPY ... /code/...` to `COPY ... /mngr/code/...`.
  - Rewrite every `RUN` step that references `/code/...` (e.g. `git config --global --add safe.directory`, `cd /code/apps/system_interface/frontend && npm run build`, `uv tool install -e /code/vendor/mngr/libs/mngr`, `ln -sf /code/vendor/tk/ticket /root/.local/bin/tk`, the `RUN mkdir -p /worktree`) so paths become `/mngr/code/...` and `/mngr/worktree/`.
  - Add an early, single layer that creates the safety-net symlinks: `ln -s /mngr/code /code` and `ln -s /mngr/worktree /worktree`. These live near the top of the Dockerfile so they exist before any later step might reference `/code/...` and so they're cached aggressively.
  - At the end of the build, after all RUN/COPY steps have populated `/mngr/code/`, rename `/mngr/code` to `/docker_build_code` so the runtime volume mount at `/mngr/` is not shadowed by image-layer content.
  - Replace the existing `CMD ["sh", "-c", "trap 'exit 0' TERM; tail -f /dev/null & wait"]` with a CMD that inlines the seed-on-first-boot logic before the trap/wait. The seed step:
    - If `/mngr/code` exists: skip seeding (idempotent restart).
    - Else if `/mngr/code.moving` exists: wipe it (`rm -rf`) and fall through to re-seed.
    - Else: require `/docker_build_code` to exist; if missing, error and exit nonzero.
    - Copy `/docker_build_code` -> `/mngr/code.moving` (cross-filesystem copy onto the volume, preserving mode/owner/timestamps).
    - On success, atomic-rename `/mngr/code.moving` -> `/mngr/code`.
    - Clean up `/docker_build_code`.
    - `mkdir -p /mngr/worktree` (idempotent, every boot).
    - Then run the existing `trap 'exit 0' TERM; tail -f /dev/null & wait` exactly as today.

- Repo-wide rewrite of `/code/...` -> `/mngr/code/...` and `/worktree/...` -> `/mngr/worktree/...` across all FCT-owned files, including:
  - `scripts/` (e.g. `create_reviewer_settings.sh`, `run_ttyd.sh`, git hooks under `scripts/git_hooks/`).
  - `services.toml` (any service command lines that reference workspace paths).
  - `.agents/skills/**` — shipped skill scripts, `SKILL.md` docs, README/asset files.
  - `apps/system_interface/` source and configs.
  - `apps/minds/...` source (FCT side, not vendor).
  - `libs/runtime_backup/`, `libs/telegram_bot/`, `libs/bootstrap/`, `libs/app_watcher/`, `libs/cloudflare_tunnel/`, `libs/web_server/` source and configs.
  - `CLAUDE.md`, `README.md`, and other top-level docs.
  - Test files (unit, integration, acceptance, release, deployment): every assertion or hardcoded path that referenced `/code/...` becomes `/mngr/code/...`, including the `target_path == "/code/"` check in `test_mngr_template_stacking.py`.
  - Docstrings and comments are rewritten too (the symlink covers stale text, but we want the docs to match reality).

- Out of scope / explicitly excluded:
  - `vendor/mngr/` — left untouched (it's a vendored upstream that follows its own convention; will be refreshed later via a normal vendor update).
  - Existing live deployed hosts — not auto-migrated; documented as a manual-migration concern.
  - The `CLAUDE_CONFIG_DIR` flow needs no special handling beyond the general path rewrite (it falls out of the workspace path change).
  - No new verification/test-plan section beyond what's covered by the affected tests being rewritten and passing.
