# Faster minds workspace build

## Overview

- Local minds workspace creation is slow because the FCT `Dockerfile`'s `COPY . /code/` invalidates every step that comes after it — `npm ci`, `npm run build`, `uv tool install`, `uv sync --all-packages`, and the playwright + chromium install — so any code edit triggers all of them.
- Split the Dockerfile so dependency-install layers only depend on dependency manifests, not on application source: a small early-`COPY` layer brings in `pyproject.toml`/`uv.lock`/per-workspace-member `pyproject.toml`/frontend `package.json` + lockfile, pre-warms the uv wheel cache and runs `npm ci`, and only then does the full `COPY . /code/` happen.
- Defer playwright (CLI + chromium browser + its apt system libs) entirely into a new `services.toml` entry `deferred-install`, run once on first container boot by the existing bootstrap service manager, gated by a container-local marker file so it never re-runs (and never silently upgrades) on subsequent restarts.
- Adopt the mngr-style "`.gitignore` is the source of truth" pattern: `.dockerignore` becomes a symlink to `.gitignore`, all FCT `.gitignore` patterns get rewritten to start with `**/` (or contain a path separator), and a new ratchet test enforces the convention. `.git/` stays in the build context — keeping full history inside the container is intentional.
- Out of scope: BuildKit cache mounts, multi-stage builder split, mirroring to the lima `extra_provision_command`, automated build-time regression tests, deferring anything beyond playwright (modal CLI, apt convenience tools), and the future "ship a shallow `.git` then fetch the rest" optimization.

## Expected Behavior

- A `docker build` after editing only application code (no manifest changes) reuses the cached Python dep, frontend dep, and (still-pre-COPY) playwright-free layers; only the post-`COPY` `uv sync --all-packages` (registering editables) and `npm run build` re-run.
- A `docker build` after editing only `vendor/mngr/` source (without changing any `vendor/mngr/libs/*/pyproject.toml`) reuses the warmed wheel cache; the post-`COPY` workspace sync only has to re-register the mngr editable packages, not re-download their deps.
- A `docker build` after bumping a manifest (`pyproject.toml`, `uv.lock`, a workspace-member `pyproject.toml`, `package.json`, or the frontend lockfile) invalidates the early-deps layer and reinstalls — but only when that actually happened.
- The container starts and the bootstrap, web, cloudflared, app-watcher, runtime-backup, and system_interface services come up without waiting on playwright/chromium.
- On the very first container boot, the bootstrap manager starts a `svc-deferred-install` tmux window that runs `playwright install --with-deps chromium` and writes `/var/lib/minds/deferred-install/done.playwright`. The window exits cleanly once finished.
- On every subsequent container boot (same image), `svc-deferred-install` starts, sees the marker, and exits 0 immediately. Playwright and chromium are not touched; no silent version drift.
- A fresh image rebuild (e.g. after a Dockerfile change) wipes the container-local marker, so the deferred install runs exactly once on the new image's first boot.
- If an agent or test tries to use playwright before the first-boot install has finished, it fails loudly — that is acceptable. FCT's `CLAUDE.md` tells the agent that some packages install asynchronously and where the marker file lives so it can wait or retry deliberately.
- `docker build` no longer sends `.venv/`, `node_modules/`, `__pycache__/`, or other ignored directories to the daemon, because `.dockerignore` now resolves (via symlink) to the rewritten `.gitignore`.
- Existing minds workspace creation, chat agent boot, and the lima provisioning flow remain functionally unchanged. Lima continues to install playwright inline via its `extra_provision_command` (a separate follow-up).

## Changes

### Dockerfile (`forever-claude-template/Dockerfile`)

- Remove the playwright tool install and the `playwright install --with-deps chromium` step entirely.
- Insert a new early-deps layer between the existing tool/binary installs and `COPY . /code/`:
  - Explicitly `COPY` (one line per file, preserving directory structure) every workspace manifest needed to drive a uv resolve: root `pyproject.toml`, `uv.lock`, each `libs/<pkg>/pyproject.toml`, `apps/system_interface/pyproject.toml`, and each `vendor/mngr/libs/{imbue_common,mngr,mngr_claude,mngr_modal,mngr_wait,resource_guards,concurrency_group}/pyproject.toml`.
  - Run `uv sync --all-packages --frozen --no-install-workspace --no-install-local` to download and cache every third-party wheel transitively required by the workspace + the mngr tools, without installing any editable package whose source is not yet present. `--no-install-workspace` already covers the root project; `--no-install-local` is the flag that skips the `[tool.uv.sources]` path deps (the `vendor/mngr/libs/*` packages).
  - `COPY` `apps/system_interface/frontend/package.json` and its lockfile and run `npm ci` in that directory.
- After the new early-deps layer, keep `COPY . /code/` (unchanged behaviour, but now lands on a much warmer cache).
- Post-`COPY` steps:
  - `npm run build` in the frontend (separate `RUN` so a frontend code edit doesn't invalidate anything Python).
  - `uv tool install -e /code/vendor/mngr/libs/mngr` and `uv tool install -e /code/apps/system_interface --with-editable /code/vendor/mngr/libs/mngr_claude --with-editable /code/vendor/mngr/libs/mngr_modal`, and the existing `mngr plugin add`.
  - `uv sync --all-packages --frozen` to register editable workspace packages into the venv against the warmed cache.
- Drop the post-`COPY` `chown -R root:root /code/` step. `COPY` without `--chown` already lands files as root:root, so the recursive chown was a no-op walk over the entire (~250 MB, including `.git/`) source tree. The `git config --global --add safe.directory /code/` part of the original `RUN` stays. This shaves ~60s off every warm-cache rebuild on top of the layer-split win.
- Drop `mngr_modal` from both the `uv tool install -e apps/system_interface --with-editable ...` chain and the `mngr plugin add --path ...` call. The FCT `.mngr/settings.toml` already sets `providers.modal.is_enabled = false`, and nothing under `apps/` or `libs/` imports `imbue.mngr_modal`, so the plugin was being installed + registered for no consumer. `mngr plugin add` shells out to a uv-tool inject per plugin, so removing one plugin saves a few seconds (~3s warm rebuild, ~4s cold-ish). Keep a comment in the Dockerfile pointing at the lines to re-add if `providers.modal` is ever flipped on.

### Deferred-install service

- Add a new `[services.deferred-install]` entry to `forever-claude-template/services.toml` whose `command` invokes a new `forever-claude-template/scripts/deferred_install.sh`.
- `scripts/deferred_install.sh`: for each deferred package (just `playwright` in this PR), if `/var/lib/minds/deferred-install/done.<package>` is absent, run the install (for playwright: `uv run playwright install --with-deps chromium`), then `mkdir -p` and `touch` the marker. If the marker is present, log and skip.
- The script uses per-package markers so future deferred packages can be added without disturbing the playwright marker.
- Marker root is container-local (`/var/lib/...`), not under `runtime/`, so it does not ride the runtime-backup branch — a same-`MNGR_AGENT_ID` re-create on a new image must trigger a single install on the new image.
- Keep the `playwright==1.58.0` pin in FCT's root `pyproject.toml` so the workspace venv has the Python wheel wired up for the test conftest; the deferred service only handles the browser binary + its apt deps.

### `.gitignore` / `.dockerignore`

- Rewrite every active pattern in FCT's `.gitignore` so it starts with `**/` or contains a path separator (matches mngr's convention so the same pattern is valid in both file formats).
- Ensure `**/node_modules/` and `**/.venv/` are present.
- Replace `.dockerignore` (currently absent) with a symlink to `.gitignore` checked into the repo.
- Add `test_gitignore_patterns_use_double_star` to FCT's `test_meta_ratchets.py` (port from the mngr meta-ratchet of the same name) and an assertion that `.dockerignore` is a symlink resolving to `.gitignore`.

### Docs

- FCT `CLAUDE.md`: short paragraph explaining that some packages (currently playwright + chromium) install asynchronously on first boot via the `deferred-install` service, with the marker-file path so the agent can wait or check status before using them.
- `forever-claude-template/libs/bootstrap/README.md`: entry documenting the new `deferred-install` service, its idempotency contract, and the marker-file convention.
- `apps/minds/docs/design.md`: one-line mention that some workspace dependencies are intentionally installed post-boot to reduce build time.

### Test updates

- New ratchet in FCT's `test_meta_ratchets.py` for the `**/` `.gitignore` convention plus the `.dockerignore` symlink assertion.
- Audit (and update if needed) `forever-claude-template/test_mngr_template_stacking.py`'s `"playwright install" in cmd` assertion — it currently checks the lima `extra_provision_command`, which is unchanged, so this should still pass; the spec call-out is to verify it's not also checking the Dockerfile.
- No automated speedup regression test; verification is manual (rebuild after a code-only edit, confirm the heavy layers come from cache).
