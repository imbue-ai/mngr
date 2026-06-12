# Releasing a new minds.app

A release ships three pinned artifacts together:

| Artifact | Pinned where |
|---|---|
| mngr code | a SHA on the release branch (`main`, or a release/* branch if cutting from a divergent state) |
| FCT template | the `v<version>` annotated tag on the `pilot` branch of `forever-claude-template` |
| Built `.app` bundle | a ToDesktop build keyed by that mngr SHA |

The binary clones the FCT tag at runtime via `FALLBACK_BRANCH` baked into `apps/minds/imbue/minds/desktop_client/templates.py`. Tag immutability is what makes a shipped binary always clone the snapshot it was verified against, even as `pilot` keeps moving.

A release iterates on a single version (e.g. `v0.2.35`) by re-cutting the FCT tag at progressively newer pilot SHAs and rebuilding the binary. The version only bumps when you decide to call a build "shipped".

## File reference

| What | Where |
|---|---|
| Version string | `apps/minds/package.json` `version` |
| Baked FCT tag | `apps/minds/imbue/minds/desktop_client/templates.py` `FALLBACK_BRANCH` |
| Local FCT pilot worktree | `/Users/weishi/Developer/imbue/forever-claude-template` on `pilot` |
| Build CI workflow | `.github/workflows/minds-launch-to-msg.yml` (`workflow_dispatch`) |
| Cold-launch smoke | `.github/workflows/minds-macos-launch.yml` (auto on push) |
| Traditional CI | `.github/workflows/ci.yml` (auto on push) |

`gh auth token --user weishi-imbue` is the imbue-org token; export it as `GH_TOKEN` for the whole release session so all `gh` calls hit the right account.

## Procedure

### 1. Set version and the target FCT tag

If shipping the current version unchanged (iteration), leave both alone. If bumping:

- Edit `apps/minds/package.json` `version` to the new value, e.g. `0.2.36`.
- Edit `apps/minds/imbue/minds/desktop_client/templates.py` `FALLBACK_BRANCH` to `"v0.2.36"`.
- Commit both together; push.

### 2. Get traditional CI green on the release branch

Wait for `ci.yml` on the release-branch HEAD to be all-green:

```bash
gh run list --workflow=ci.yml --branch=wz/minds_onboard --limit=1 \
  --json databaseId,headSha,status,conclusion
```

Expected jobs: `test-offload`, `test-docker`, `test-docker-electron`, `test-offload-acceptance`. All must succeed.

### 3. Refresh FCT pilot's `vendor/mngr/` from the green mngr SHA

```bash
export MNGR=/Users/weishi/Developer/imbue/mngr
export FCT=/Users/weishi/Developer/imbue/forever-claude-template

cd "$FCT"
git switch pilot
git pull --ff-only origin pilot

rm -rf vendor/mngr && mkdir -p vendor/mngr
(cd "$MNGR" && git archive HEAD) | tar -x -C vendor/mngr

git add -A
git commit -m "vendor/mngr: refresh from wz/minds_onboard $(git -C "$MNGR" rev-parse --short HEAD)"
```

`git archive HEAD | tar -x` mirrors tracked files only — no `__pycache__`, `uv.lock`, `.venv`, or other working-tree cruft. Do not exclude `apps/minds/`; the pilot needs it.

### 4. Push pilot and re-cut the tag

```bash
export GH_TOKEN=$(gh auth token --user weishi-imbue)
git push https://x-access-token:$GH_TOKEN@github.com/imbue-ai/forever-claude-template.git pilot

VERSION=v0.2.36   # whatever you're shipping
git tag -d "$VERSION" 2>/dev/null || true
git tag -a "$VERSION" HEAD -m "minds binary $VERSION: pilot $(git rev-parse --short HEAD) (vendor/mngr from wz/minds_onboard $(git -C "$MNGR" rev-parse --short HEAD))"
git push --force https://x-access-token:$GH_TOKEN@github.com/imbue-ai/forever-claude-template.git refs/tags/"$VERSION"
```

The tag must be **annotated** (`-a`) — a lightweight tag won't carry the message and breaks downstream tooling that inspects tag objects.

### 5. Trigger `minds-launch-to-msg.yml` on the mngr SHA × FCT tag

```bash
cd "$MNGR"
MNGR_SHA=$(git rev-parse HEAD)
gh workflow run minds-launch-to-msg.yml -R imbue-ai/mngr \
  -r wz/minds_onboard \
  -f commit_sha="$MNGR_SHA" \
  -f template_ref="$VERSION"
```

Run this from inside the mngr checkout — `gh workflow run` resolves the repo from `cwd`'s remote when `-R` is parsed inconsistently. From the FCT checkout, this call has hit a 404 while still creating the run, producing a duplicate.

The workflow has two jobs:
- `build` packages a ToDesktop bundle for `$MNGR_SHA` (reuses an existing build if one already matches via `versionControlInfo.commitId`; otherwise fresh).
- `verify` downloads the bundle on a fresh self-hosted Mac, launches it, creates an agent against FCT `$VERSION`, sends a first message, asserts the round-trip.

Wait for both green. Note the build ID printed in the `build` summary — it's the ToDesktop bundle to ship.

### 6. Local dev-build verification (inner loop)

Drive the dev build with the latest mngr code against the same FCT tag. Operator clicks through Electron manually; the goal is to catch anything CI's headless Playwright path missed. See `apps/minds/.claude/skills/minds-dev-iterate/SKILL.md` for the dev-iteration loop.

### 7. Optionally drive the ToDesktop bundle locally

Download the zip from the build URL printed in step 5:

```
https://dl.todesktop.com/26032588hqdzk/builds/<build_id>/mac/zip/arm64
```

Replace `/Applications/Minds.app` with it, quit any running minds first, then launch and run through create-agent → first message. This catches any release-vs-dev bundling drift before publishing.

### 8. Ship

ToDesktop's `pnpm exec todesktop release` is blocked by server-side auth on this app, so the only working path is the web UI:

1. Open `https://app.todesktop.com/apps/26032588hqdzk/builds/<build_id>`.
2. Click **Release**.

Auto-updater will pick up the new build on the next user launch.

## Failure modes worth knowing

- **`test-docker-electron` aborts on `git checkout v<version>` with dirty `.mngr/settings.toml`.** The test fixture flips a pytest opt-in in the FCT shallow clone before the spawned `mngr create` does its in-place checkout. The runner now pre-checks-out the clone to `FALLBACK_BRANCH` after the tag fetch so the in-place checkout is a no-op even with the dirty file. If you bump `FALLBACK_BRANCH`, make sure the tag is reachable on FCT origin before this runs.
- **`gh workflow run` creates a duplicate run.** See step 5 — always invoke from the mngr cwd.
- **Old workflow's sidebar entry sticks after a rename.** GHA only unregisters the entry once all its runs are deleted. Disable via `PUT /repos/.../actions/workflows/{id}/disable` then `DELETE /repos/.../actions/runs/{run_id}` for each old run; the entry then disappears.
- **`mngr create` fails with "Remote branch v<version> not found".** The shallow clone in CI doesn't fetch tags by default; the runner now runs `git fetch --depth 1 --tags origin` after clone. If you see this on a fresh runner, confirm the tag was actually pushed in step 4.

## Pre-flight check before any push

`gh api user --jq .login` with `GH_TOKEN` set must print `weishi-imbue` (or whichever org-authorized account is intended). Default keychain "active" account can drift between parallel agents — never rely on it.
