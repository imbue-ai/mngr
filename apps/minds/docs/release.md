# Releasing a new minds.app

A release ships three pinned artifacts that must agree:

| Artifact | Pinned where |
|---|---|
| mngr code | a `main` SHA, tagged `minds-v<version>` |
| FCT template | the `minds-v<version>` tag on `forever-claude-template` `main` |
| `.app` bundle | a ToDesktop build keyed by that mngr SHA |

Both repos tag with the **`minds-v<version>`** prefix (e.g. `minds-v0.3.1`), namespacing minds releases from each repo's own `v<version>`. The shipped binary clones the FCT tag at runtime via `FALLBACK_BRANCH` in `apps/minds/imbue/minds/desktop_client/templates.py`; tag immutability pins a binary to the snapshot it was verified against.

Both repos release from **`main`** via **two PRs that both target `main`** (one per repo): prove the pair green, review, merge, tag each `main`, then re-prove green against the tags. **Green CI on the tags concludes the release**; clicking *Release* in ToDesktop is an optional follow-up.

## The two PRs

| Repo | Carries |
|---|---|
| `mngr` | version bump (`apps/minds/package.json`), `FALLBACK_BRANCH` (`templates.py`), any mngr/minds code |
| `forever-claude-template` | `vendor/mngr/` archived from the green mngr SHA, plus any consumer (`system_interface`) changes that vendor requires |

**Vendor-match invariant.** FCT `vendor/mngr` must be the `git archive` of the *exact* mngr SHA it's paired with — the `commit_sha` you verify and the mngr SHA you tag. The binary runs the mngr SHA; the in-VM agent imports `vendor/mngr`. If they diverge, the agent's mngr can mismatch the binary's API (how the `system_interface` → `send_message_to_agents` break slipped in). Re-archive whenever the mngr SHA changes.

> The Apple-Silicon lima-VZ `cryptography` SIGILL is handled in the FCT template by `OPENSSL_armcap=0` (`.mngr/settings.toml` `host_env__extend` + `scripts/build_workspace.sh`), which skips OpenSSL's SVE CPU-cap probe. mngr does not pin `cryptography`.

**Reviewing the FCT PR.** Two kinds of change land on the same branch: the mechanical `vendor/mngr` snapshot (hundreds of files) and reviewable code (e.g. a `system_interface` fix). CI needs the *full* branch — the binary clones the ref and imports the committed `vendor/mngr` — but reviewers should read only the code. Keep them separable:

1. **Isolate the vendor refresh in its own commit** (`vendor/mngr: refresh from mngr <sha>`), distinct from the reviewable commits, so review can be done per-commit.
2. **`vendor/mngr/**` is `linguist-generated` in `.gitattributes`**, so GitHub collapses it in the PR "Files changed" view by default — reviewers see only the real changes.
3. **The snapshot is verified by reproduction, not review**: `git archive <sha> | diff -r vendor/mngr` (the vendor-match check) proves it equals the tagged mngr SHA. A clean diff *is* the review.

## File reference

| What | Where |
|---|---|
| Version string | `apps/minds/package.json` `version` |
| Baked FCT tag | `apps/minds/imbue/minds/desktop_client/templates.py` `FALLBACK_BRANCH` |
| Local checkouts | `/Users/weishi/Developer/imbue/{mngr,forever-claude-template}` |
| Build / e2e CI | `.github/workflows/minds-launch-to-msg.yml` (`workflow_dispatch`) |
| Traditional CI | `.github/workflows/ci.yml` (auto on push) |

Export the imbue-org token for the whole session: `export GH_TOKEN=$(gh auth token --user weishi-imbue)`. Pre-flight any push with `gh api user --jq .login` → must print `weishi-imbue` (the keychain "active" account drifts between parallel agents).

## Procedure

### 1. Bump version + FALLBACK_BRANCH (mngr PR)

For an iteration of the same version, skip. To bump: set `apps/minds/package.json` `version` (e.g. `0.3.1`) and `templates.py` `FALLBACK_BRANCH` to `"minds-v0.3.1"`. This bakes in a tag that doesn't exist until step 7 — fine, because step 4 overrides the FCT ref via `template_ref`, so the tag is only hit in step 8.

### 2. Traditional CI green on both PR branches

`ci.yml` must be all-green on each PR HEAD (mngr jobs: `test-offload`, `test-docker`, `test-docker-electron`, `test-offload-acceptance`).

### 3. Refresh FCT `vendor/mngr` from the green mngr SHA (FCT PR)

On the FCT PR branch (cut from `origin/main`, clean tree), with the **mngr checkout positioned at the green SHA from step 2** (i.e. on the mngr release PR branch), run the sync recipe:

```bash
just sync-vendor-mngr /Users/weishi/Developer/imbue/forever-claude-template
cd /Users/weishi/Developer/imbue/forever-claude-template && git push
```

`just sync-vendor-mngr` does `git archive HEAD` → FCT `vendor/mngr` (tracked files only; keep `apps/minds/`) and commits `Sync vendor/mngr to <branch> (<short>)`; it aborts if FCT is dirty and does not push. If the new vendor changes an mngr API a consumer calls (e.g. `system_interface`), fix that consumer in this same PR.

### 4. Prove the pair green pre-merge

The tag doesn't exist yet, so pass the FCT PR branch as `template_ref`. `commit_sha` and that branch's `vendor/mngr` must be the same mngr SHA.

```bash
cd "$MNGR"
gh workflow run minds-launch-to-msg.yml -R imbue-ai/mngr \
  -r <mngr-pr-branch> -f commit_sha="$GREEN_MNGR_SHA" -f template_ref=<fct-pr-branch>
```

`build` packages/reuses (keyed by `commit_sha`) the bundle; `launch_to_msg` launches it, creates an agent from the FCT ref, sends a first message, asserts the round-trip. Invoke from the mngr cwd — from the FCT cwd it has 404'd mid-create and duplicated the run.

### 5. Review and approve both PRs

Still branch refs; nothing tagged yet.

### 6. Merge both PRs to `main`

**Merge the mngr PR with a merge commit, not a squash.** `main` can advance past the SHA you built and verified in step 4 (`$GREEN_MNGR_SHA`) while you were verifying; a merge commit keeps that exact SHA reachable on `main` as a parent (a squash replaces it with a new commit whose tree also contains the drift — and the binary you verified was built from neither).

The tag pins **`$GREEN_MNGR_SHA`** — the SHA the binary was built from and FCT's `vendor/mngr` was archived from — **not** `main`'s HEAD. Confirm the vendor still matches *that* SHA:

```bash
GREEN_MNGR_SHA=<the SHA from step 4>
TMP=$(mktemp -d); (cd "$MNGR" && git archive "$GREEN_MNGR_SHA") | tar -x -C "$TMP"
diff -r "$TMP" /Users/weishi/Developer/imbue/forever-claude-template/vendor/mngr && echo OK || echo "MISMATCH — FCT vendor was not archived from $GREEN_MNGR_SHA (re-run step 3)"
```

`git archive main` (HEAD) failing to match while `git archive $GREEN_MNGR_SHA` matches is **expected drift** (unrelated PRs landed on `main`), not an error — tag `$GREEN_MNGR_SHA`, not HEAD.

### 7. Tag the verified pair — *not* `main` HEAD

Tag mngr at **`$GREEN_MNGR_SHA`** (the built+verified SHA; reachable on `main` as the merge parent) and FCT at the commit whose `vendor/mngr` is that SHA's archive (the FCT PR's merge into `main`):

```bash
export GH_TOKEN=$(gh auth token --user weishi-imbue)
export MNGR=/Users/weishi/Developer/imbue/mngr FCT=/Users/weishi/Developer/imbue/forever-claude-template
VERSION=minds-v0.3.1
GREEN_MNGR_SHA=<the SHA from step 4>
git -C "$FCT" fetch origin --quiet; FCT_SHA=$(git -C "$FCT" rev-parse origin/main)   # its vendor/mngr == archive $GREEN_MNGR_SHA

git -C "$MNGR" tag -a "$VERSION" "$GREEN_MNGR_SHA" -m "minds $VERSION: mngr $(git -C "$MNGR" rev-parse --short $GREEN_MNGR_SHA) / FCT $(git -C "$FCT" rev-parse --short $FCT_SHA) (vendor/mngr from mngr $GREEN_MNGR_SHA)"
git -C "$MNGR" push https://x-access-token:$GH_TOKEN@github.com/imbue-ai/mngr.git refs/tags/"$VERSION"

git -C "$FCT" tag -a "$VERSION" "$FCT_SHA" -m "minds $VERSION: FCT $(git -C "$FCT" rev-parse --short $FCT_SHA) / mngr $(git -C "$MNGR" rev-parse --short $GREEN_MNGR_SHA) (vendor/mngr from mngr $GREEN_MNGR_SHA)"
git -C "$FCT" push https://x-access-token:$GH_TOKEN@github.com/imbue-ai/forever-claude-template.git refs/tags/"$VERSION"
```

Tags must be annotated (`-a`). **Tag the verified SHA, never `main` HEAD** — between step 4 and the merge, `main` can pick up unrelated commits never built into the binary or run through launch-to-msg (e.g. `main` HEAD once sat +58 such files past the tagged SHA). To re-cut during iteration: `git tag -d "$VERSION"` then `git push --force ... refs/tags/"$VERSION"`.

### 8. Close the loop: CI on the two tags

Both refs = the tag, exercising the binary's baked `FALLBACK_BRANCH` end to end. Because the mngr tag is the step-4 SHA, `build` reuses the bundle you already verified:

```bash
cd "$MNGR"; VERSION=minds-v0.3.1
gh workflow run minds-launch-to-msg.yml -R imbue-ai/mngr \
  -r main -f commit_sha="$VERSION" -f template_ref="$VERSION"
```

**Green here concludes the release.** Note the build ID in the `build` summary.

### 9. Optional: dev verify + ship

Drive the build's ToDesktop zip (`https://dl.todesktop.com/26032588hqdzk/builds/<build_id>/mac/zip/arm64`, replaces `/Applications/Minds.app`) or the dev build through create-agent → first message. To publish, click **Release** at `https://app.todesktop.com/apps/26032588hqdzk/builds/<build_id>` (the `todesktop release` CLI is auth-blocked); auto-updater picks it up on next launch.

## Failure modes worth knowing

- **`test-docker-electron` aborts on `git checkout minds-v<version>` with dirty `.mngr/settings.toml`.** The runner pre-checks-out the FCT clone to `FALLBACK_BRANCH` after the tag fetch so the in-place checkout is a no-op. If you bump `FALLBACK_BRANCH`, the tag must be reachable on FCT origin (step 7) first.
- **`gh workflow run` creates a duplicate run.** Always invoke from the mngr cwd (step 4).
- **`mngr create` fails "Remote branch minds-v<version> not found".** The CI shallow clone runs `git fetch --depth 1 --tags origin`; if it still fails on a fresh runner, confirm the tag was pushed (step 7).
- **Renamed workflow's sidebar entry sticks.** GHA unregisters only once all its runs are deleted: `PUT .../workflows/{id}/disable`, then `DELETE .../runs/{run_id}` for each.
