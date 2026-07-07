# Paired FCT workspace tests

## Overview

- Remote workspace-creation tests (the minds snapshot bake + resume, the create+chat acceptance test, the full-flow harness) currently run FCT pinned to the released `minds-v0.3.4` tag, so a coordinated mngr+FCT change (e.g. removing the `workspace` label + its `has(labels.workspace)` list filter) can never be tested together — the tag still carries the old FCT, and the container's in-`mngr list` hides the now-unlabeled `system-services`.
- Fix this by reproducing the `just minds-start` local-debug state in CI: materialize a proper FCT checkout at `_FCT_EXTERNAL_WORKTREE` on the **paired branch** (the FCT branch whose name matches the current mngr branch, else FCT `main`), with the branch-under-test's mngr freshly vendored into `vendor/mngr`, and point the create flow at it via the existing `MINDS_WORKSPACE_*` env vars.
- Do the materialize **where git works** (CI runner before staging, or a local setup) and bake the worktree into the snapshot image, so the sandboxes already have the right code — no in-sandbox clone, no `GITHUB_HEAD_REF` forwarding, no git-independent vendoring workarounds.
- Keep this **test-setup-only**: the application already honors `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS` / `MINDS_WORKSPACE_GIT_URL` / `MINDS_WORKSPACE_BRANCH`, so nothing in the create form, `agent_creator`, `FALLBACK_BRANCH`, production, `pool.py`/`admin.py`, or the operator `minds-start` flow changes.
- Tests strictly **consume** the worktree (error if absent); a separate materialize step **produces** it. This eliminates the "did we forget to re-vendor the FCT branch?" failure mode and lets simultaneous mngr+FCT changes go green together.

## Expected behavior

- When the current mngr branch has a same-named FCT branch, workspace-creation tests build containers from that FCT branch's content plus the branch's mngr; when it does not, they build from FCT `main` plus the branch's mngr.
- The released `minds-v0.3.4` tag is never what these tests bake (the prep always sets `MINDS_WORKSPACE_BRANCH`); production is unaffected and still pins the tag (and already re-vendors mngr at release).
- The current `mngr/simple-names` change goes green: its paired FCT branch drops the `has(labels.workspace)` filter, so the baked container's `mngr list` shows `system-services` and the snapshot resume assertion passes.
- Routine mngr PRs (no paired FCT branch) now exercise FCT `main` + vendored HEAD mngr instead of the stale tag — accepted, since real integration coverage is the goal; a broken/incompatible FCT `main` will surface in these tests.
- The materialize step runs where git works and is skipped when a worktree is already present (an operator's `minds-start` worktree is never clobbered); the tests themselves error clearly if no worktree exists, rather than silently doing prep or falling back to the tag.
- Genuine prep failures (branch name undeterminable, clone of the resolved ref fails, vendoring fails) hard-fail loudly; "no paired branch" is the normal `main` case, not a failure.
- Local `just minds-test-electron` runs the materialize first (via its recipe), so it "just works"; a bare test invocation with no worktree errors with a pointer to the setup.

## Changes

- Add a materialize/setup step (a shared, unit-testable function) that, when `_FCT_EXTERNAL_WORKTREE` is absent, resolves the mngr branch (`GITHUB_HEAD_REF` → `_current_mngr_branch`), clones the paired FCT branch or FCT `main`, checks it out, vendors the branch's mngr into `vendor/mngr` (reusing the existing git-based `sync-vendor-mngr` path), and writes the `is_allowed_in_pytest` opt-in; when present, it leaves the worktree untouched.
- Have the minds snapshot bake run this materialize on the runner **before staging**, and stage the resulting `_FCT_EXTERNAL_WORKTREE` into the sandbox via a **separate upload path** (the main staging rsync keeps excluding `.external_worktrees/`), so the worktree is baked into the snapshot image and inherited by the resume sandbox.
- Make the workspace-creation entry points consume the worktree strictly: keep `resolve_fct_path` as-is (its step-1 external-worktree short-circuit returns the baked worktree), and **error when `_FCT_EXTERNAL_WORKTREE` is absent** instead of prepping inside a test.
- Drop the pytest create+chat path's `materialize_isolated_fct` / tmp-isolation (which re-clones and re-pins the `minds-v0.3.4` tag and discards the uncommitted vendoring) so it uses the baked worktree directly; the test no longer writes the opt-in itself.
- Extend the existing env-defaults setter (`ensure_minds_env_defaults`, via its `setenv` strategy) to also set `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS`, `MINDS_WORKSPACE_GIT_URL`, and `MINDS_WORKSPACE_BRANCH` by reading the present worktree's path and current branch.
- Wire the local `just minds-test-electron` recipe to run the materialize step first; leave `minds-start`, `pool.py`/`admin.py` vendoring, `FALLBACK_BRANCH`, and all application code unchanged.
- Document (in the snapshot/e2e test infra) that these tests intentionally run the paired FCT branch (or `main`) with freshly-vendored mngr, and that the tag is never baked — no separate self-verification assertion is added.
- No teardown of the materialized worktree (the environments are ephemeral).
- Add the required changelog entry under the touched project(s) (e.g. `apps/minds/changelog/<branch>.md`, and `dev/changelog/<branch>.md` if CI workflow files change).
