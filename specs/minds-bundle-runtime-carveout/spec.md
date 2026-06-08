# Carve "minds bundled-app runtime fixes" out of PR #1317

## Goal

PR #1317 (`wz/minds_onboard`) carries ~1900 LoC of Electron-side runtime fixes that the bundled minds.app needs in order to launch correctly on macOS and stay current across upgrades. The rest of #1317 is unrelated (mngr_forward subdomain self-heal, e2e iters, FCT pilot ratchets, etc).

Reviewers asked for a smaller, focused PR for the bundle work alone. This spec describes how to carve it out.

The goal of this carve-out PR is a green ToDesktop build whose runtime behavior matches the wz/minds_onboard branch on every bundled-app concern (uv venv writability, workspace package staleness, restic discovery, PATH, About panel) — without touching mngr_forward, e2e, FCT pilot, or the spec/docs in #1317.

Branch from `origin/main` (NOT `wz/minds_onboard`). Target the new PR at `origin/main`. The carve-out must land first or alongside the remaining #1317 reduction; the remaining #1317 PR can be rebased afterwards.

## What's in scope

Every change on `wz/minds_onboard` whose effect is on the bundled minds.app at runtime, plus the build pipeline that produces it. Files (verify file list against the live diff at carve-out time):

### Electron shell (the runtime)
- `apps/minds/electron/main.js` — About panel git SHA, DevTools env-var escape hatch, electron destructured-imports cleanup.
- `apps/minds/electron/backend.js` — `uv run --active` flag, `VIRTUAL_ENV` set to per-user venv, `MINDS_RESTIC_BINARY` env, PATH augmentation with `/opt/homebrew/bin` and `/usr/local/bin`, `-v` flag on the spawned `minds` to lift INFO into `minds.log`.
- `apps/minds/electron/env-setup.js` — `--active` flag on `uv sync`, `WORKSPACE_PACKAGES` reinstall list (the four-place mirrored constant).
- `apps/minds/electron/paths.js` — `getResticPath()` helper.
- `apps/minds/electron/pyproject/pyproject.toml` — workspace package list (direct dependencies + `[tool.uv.sources]` overrides), updated `exclude-newer` docstring.

### Build pipeline
- `apps/minds/scripts/build.js` — restic download path, workspace package list (the same four-place mirrored constant), supply-chain cooldown wiring.
- `apps/minds/scripts/build_test.py` — drift guard `test_workspace_package_lists_are_consistent` that asserts the four list sources agree; restic-bundled assertion; entitlement skip when node is missing.
- `apps/minds/scripts/download-binaries.js` — restic per-platform downloader.
- `apps/minds/scripts/ensure-binaries.js` — pre-launch dev check that the restic binary is present.

### Package manifest
- `apps/minds/package.json` — any `prestart`/`predist` hook + dependency changes that flow from the above.
- `apps/minds/pnpm-workspace.yaml` — `minimumReleaseAge` and the latchkey exemption (if changed).
- `apps/minds/pnpm-lock.yaml` — regenerated lockfile.

### Changelogs
- `apps/minds/changelog/<branch-name>.md` per CLAUDE.md changelog rule.
- `apps/minds/CHANGELOG.md` and `apps/minds/UNABRIDGED_CHANGELOG.md` should NOT be touched; the nightly consolidation agent owns those.

## What's NOT in scope (stays in the remaining #1317 PR)

- `libs/mngr_forward/imbue/mngr_forward/server.py` (subdomain self-heal `/goto/` redirect)
- `libs/mngr_forward/imbue/mngr_forward/server_test.py` (subdomain self-heal tests)
- `libs/mngr_lima/imbue/mngr_lima/limactl.py` (restored 1800s timeout)
- `apps/minds/scripts/launch_to_msg_e2e.py` (the e2e driver — only the bundle build runs in CI, not the e2e)
- `apps/minds/scripts/mac-runner-reset.sh` (mac-runner cleanup)
- `.github/workflows/minds-launch-to-msg.yml` (mac-runner CI workflow)
- `specs/minds-platform-canonical-dirs/spec.md` (architecture spec, separate work)
- Any `apps/minds/imbue/minds/...` Python code outside what the bundle wiring touches
- FCT pilot changes (different repo)

If you find a file that doesn't clearly fit either side, leave it in the remaining #1317 PR.

## Why the carve-out

1. **Reviewability.** The bundle work is concrete, mechanical, and uncoupled from the protocol-level changes in the rest of #1317. Reviewers can verify each piece (venv writability, package staleness, restic, PATH) in isolation without paging in mngr_forward auth.
2. **Risk isolation.** Bundle-runtime changes either work (the .app launches) or don't (it doesn't). The mngr_forward + e2e changes have a different failure surface (security boundaries, race conditions). Carving them apart lets each PR fail independently.
3. **Bisectability.** If `git bisect` later flags one of these as the cause of a regression, the bisect lands on a focused commit instead of a 2k-LoC merge.
4. **Reuse.** The bundle-runtime fixes are not specific to the wz/minds_onboard mission; they're maintenance on the desktop app. Future hotfix PRs that want some of these (e.g. only the About-panel git SHA) can branch off this carve-out.

## Risks and how to handle them

### The 4-place `WORKSPACE_PACKAGES` invariant must agree

`apps/minds/electron/env-setup.js`, `apps/minds/electron/pyproject/pyproject.toml`, `apps/minds/scripts/build.js`, and `apps/minds/scripts/build_test.py` all carry copies of the workspace package list. `test_workspace_package_lists_are_consistent` in `build_test.py` is the drift guard. The carve-out PR MUST contain all four updates or that test fails.

When verifying the carve-out diff locally, run that specific test before pushing:

```
just test-quick apps/minds/scripts/build_test.py::test_workspace_package_lists_are_consistent
```

### Bundle CI must stay green on the remaining #1317

After this PR merges, rebase `wz/minds_onboard` onto main. Some merge conflicts in changelogs are expected; resolve by keeping the per-branch entry. The remaining branch should still produce a green `minds-launch-to-msg` run.

If the remaining #1317 was previously relying on a bundle-runtime fix that you didn't carry over (e.g. the e2e expected the new `MINDS_RESTIC_BINARY` env), CI will fail with a specific error — fix it on the remaining branch, not by re-merging this PR into it.

### supply-chain cooldown setting

`pnpm-workspace.yaml`'s `minimumReleaseAge` and `pyproject.toml`'s `exclude-newer` got toggled several times during #1317 development (the WIP commits `bb74f8a43` and `c930b05ed`). The final committed value on `wz/minds_onboard` is the intended end state; replicate that, not any intermediate value. The matching latchkey exemption in `pnpm-workspace.yaml` lives next to `minimumReleaseAge` and must be carried over together.

### Lima bundling is its own merge artifact

PR `mngr/minds-bundle-lima` was merged into `wz/minds_onboard` (commit `508d22aa4`). The lima-bundling changes (`apps/minds/scripts/build.js` lines that download + strip Lima, `apps/minds/electron/paths.js` `getLimaPath`, etc) are part of that earlier merge, not the bundle-runtime carve-out. If you find Lima-related lines in the diff, check whether they were already on `origin/main` at the time of carve-out — they probably are.

## Concrete plan

1. Branch off the latest `origin/main`: `git switch -c mngr/minds-bundle-runtime origin/main`.
2. Cherry-pick or re-apply the in-scope changes from `wz/minds_onboard`. The mechanical way:
   - For each file in the scope list, `git checkout wz/minds_onboard -- <path>` to copy the wz/minds_onboard version onto your branch.
   - Then prune anything in those files that crept in from out-of-scope concerns (rare; the files listed above are dominated by bundle-runtime work).
3. Add the per-PR changelog entry at `apps/minds/changelog/mngr-minds-bundle-runtime.md` describing the carved-out scope.
4. Run the workspace-package-list drift guard locally (see above).
5. Run the full Mac-runner CI on the branch: it has to produce a green ToDesktop build and a launch-smoke pass on `macos-launch.yml`. (`launch-to-msg.yml` is the mac-runner e2e; that workflow exercises the bundled app end-to-end. If it stays green for the carve-out, the bundle is healthy.)
6. Open the PR titled `minds: carve bundled-app runtime fixes out of #1317`, target `main`, link `#1317` in the body, and list the carved-out scope.

## Acceptance criteria

1. The new PR's diff vs `origin/main` is a strict subset of `wz/minds_onboard` vs `origin/main`. No new code, no rewrites.
2. `test_workspace_package_lists_are_consistent` passes on the new branch.
3. The CI matrix for the new branch is green:
   - `ci.yml` (`test-offload`, `test-docker`, `test-docker-electron`, `test-offload-acceptance`).
   - `minds-macos-launch.yml` (vanilla macos-launch).
   - `minds-launch-to-msg.yml` build + verify against the new branch's HEAD.
4. After this PR merges and `wz/minds_onboard` is rebased onto main, the rebased #1317 still produces a green `minds-launch-to-msg.yml`. (Run it once to confirm before the rebase ships.)
5. The PR body lists each in-scope file and why it's part of "bundled-app runtime fixes."

## Out of scope for this carve-out (future work)

- Reorganizing the four-place `WORKSPACE_PACKAGES` invariant into a single source of truth. The drift guard is fine for now; this is a future cleanup.
- Migrating the per-user venv path off `~/.minds/.venv` — that's tracked by `specs/minds-platform-canonical-dirs/spec.md`.
- Re-evaluating the supply-chain cooldown values (`14 days` etc) — leave as-is.
- Any change to FCT (`forever-claude-template`) — different repo.

## How to verify the carve-out is "clean"

After your branch is ready, before pushing, compare the two diffs:

```
git diff origin/main..mngr/minds-bundle-runtime > /tmp/carveout.diff
git diff mngr/minds-bundle-runtime..wz/minds_onboard > /tmp/leftover.diff
```

`carveout.diff` should ONLY contain in-scope files. `leftover.diff` should ONLY contain out-of-scope files. If a file appears in both, the carve was imperfect: either pull more of it into `carveout.diff` (if the whole file belongs there) or push it out (if some of its lines were in-scope and got moved correctly while others stayed).
