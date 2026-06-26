# Releasing a new minds.app

A release ships three pinned artifacts that must agree:

| Artifact | Pinned where |
|---|---|
| mngr code | a `main` SHA, tagged `minds-v<version>` |
| FCT template | the `minds-v<version>` tag on `forever-claude-template` `main` |
| `.app` bundle | a ToDesktop build keyed by that mngr SHA |

Both repos tag with the **`minds-v<version>`** prefix (e.g. `minds-v0.3.1`), namespacing minds releases from each repo's own `v<version>`. The shipped binary clones the FCT tag at runtime via `FALLBACK_BRANCH` in `apps/minds/imbue/minds/desktop_client/templates.py`; tag immutability pins a binary to the snapshot it was verified against.

Both repos release from **`main`**. Neither `main` is branch-protected, so a PR is **never a merge gate** — you can push or merge to `main` directly. Its only role here is as a **CI surface**: `ci.yml` runs on PRs (any branch) and on push to `main`, *never on a bare branch push*, so opening a PR is how you get traditional CI on a release branch. Nothing is opened for human *review* unless real code rides along. Each repo gets a short-lived **release branch** (mngr: the version bump; FCT: the `vendor/mngr` refresh); you prove the pair green, land both on `main`, tag each `main`, then re-prove green against the tags. **Green CI on the tags concludes the release**; clicking *Release* in ToDesktop is an optional follow-up.

## The two release branches

| Repo | Carries | Open a PR? |
|---|---|---|
| `mngr` | version bump (`apps/minds/package.json`), `FALLBACK_BRANCH` (`templates.py`), any mngr/minds code | Optional. Traditional CI on an inert bump is redundant with a green `main`, so a PR adds little — open one for a record, or when the branch also carries mngr/minds code you want CI/review on. |
| `forever-claude-template` | `vendor/mngr/` archived from the green mngr SHA, plus any consumer (`system_interface`) changes that vendor requires | Yes — as a **CI surface, not a review**. A pure vendor refresh isn't read, but a PR is the only way to run `ci.yml`'s `test` job (`uv sync` + `system_interface` tests) on the branch, which catches a `uv`-resolution or `system_interface` break fast on a big vendor jump. (You *can* skip it and lean on launch-to-msg, which covers the same end-to-end, just slower.) |

**Vendor-match invariant.** FCT `vendor/mngr` must be the `git archive` of the *exact* mngr SHA it's paired with — the `commit_sha` you verify and the mngr SHA you tag. The binary runs the mngr SHA; the in-VM agent imports `vendor/mngr`. If they diverge, the agent's mngr can mismatch the binary's API (how the `system_interface` → `send_message_to_agents` break slipped in). Re-archive whenever the mngr SHA changes. When iterating on CI this means dispatching a `template_ref` whose `vendor/mngr` is synced to the SHA you're building — never FCT `main`, which lags: a stale vendor silently rejects a field the binary renamed, so the in-VM agent never starts and the e2e wedges at "Waiting for initial chat agent…" (looks like a frontend hang, is really vendor skew; seen for `use_env_config_dir` → `isolate_local_config_dir`). `just sync-vendor-mngr` produces a matching FCT branch.

> The Apple-Silicon lima-VZ `cryptography` SIGILL is handled in the FCT template by `OPENSSL_armcap=0` (`.mngr/settings.toml` `host_env__extend` + `scripts/build_workspace.sh`), which skips OpenSSL's SVE CPU-cap probe. mngr does not pin `cryptography`.

**The FCT vendor refresh is not reviewed.** The `vendor/mngr` snapshot (thousands of files) is generated and verified by *reproduction*, not by reading: the step-6 vendor-match check (a `git ls-tree` blob-hash comparison of the FCT vendor tree against the tagged mngr SHA's tree) proves it equals that SHA file-for-file. A clean comparison *is* the review. (`vendor/mngr/**` is `linguist-generated` in FCT's `.gitattributes`, so GitHub also collapses it.) The branch exists only to (a) stage the refresh so launch-to-msg can verify the (binary, template) pair **before** it lands on `main`, and (b) be the commit the tag points at. If a `system_interface` consumer fix rides along, isolate the vendor refresh in its own commit (`vendor/mngr: refresh from mngr <sha>`) so the real code is reviewable on its own — that fix is the only part anyone reads.

## File reference

| What | Where |
|---|---|
| Version string | `apps/minds/package.json` `version` |
| Baked FCT tag | `apps/minds/imbue/minds/desktop_client/templates.py` `FALLBACK_BRANCH` |
| `forever-claude-template` checkout | `$FCT` — your local clone; `just sync-vendor-mngr` (step 3) reads its path from a gitignored `apps/minds/.env`. See Session setup. |
| `mngr` monorepo checkout | `$MNGR` — wherever you cloned it; you run `just` / `git` from here. See Session setup. |
| Build / e2e CI | `.github/workflows/minds-launch-to-msg.yml` (`workflow_dispatch`) |
| Traditional CI | `.github/workflows/ci.yml` (auto on push) |

## Session setup

Set these once for the whole session — later steps assume them:

- **`GH_TOKEN`** (derived, per session) — `export GH_TOKEN=$(gh auth token --user weishi-imbue)`. Pre-flight any push with `gh api user --jq .login` → must print `weishi-imbue` (the keychain "active" account drifts between parallel agents).
- **`MNGR`** and **`FCT`** — absolute paths to your `mngr` and `forever-claude-template` clones, used by the shell commands in steps 4/6/7: `export MNGR=/your/mngr FCT=/your/forever-claude-template`.
- **`FCT_DIR`** — the *same* `forever-claude-template` path, but consumed by `just sync-vendor-mngr` (step 3), which reads it from a gitignored `apps/minds/.env` (minds-scoped, never committed — only that recipe loads it, so no shell-rc edit and it reaches non-interactive agent shells; see `apps/minds/.env.example`):
  ```bash
  echo "FCT_DIR=$FCT" >> apps/minds/.env
  ```
  An agent: if `apps/minds/.env` doesn't already define `FCT_DIR`, ask the user for their checkout path — don't guess.

## What actually gates a release (vs. confirmation)

Three things must hold; only two need *new* CI:

1. **The binary built from the release SHA works end-to-end** — `minds-launch-to-msg.yml` (step 4). `main` never runs this, so it is the release's only unique verification and its wall-clock long pole. Start it as early as possible.
2. **The FCT PR's `test` job is green** (step 2) — real signal: it refreshes `vendor/mngr` (and may carry a `system_interface` fix), so a `uv`-resolution or stale-API break surfaces here. `ci.yml` only runs on a PR or on `main`, so this needs the FCT branch opened as a PR (a CI surface, not a review).
3. **`vendor/mngr` equals the tagged mngr SHA** — proved by reproduction (the step-6 `git ls-tree` blob-hash comparison), not by CI.

*Not* new signal: **traditional CI on a version-bump-only mngr branch.** Bumping `version` + `FALLBACK_BRANCH` can't change test behavior — no test asserts the version literal or that `FALLBACK_BRANCH` resolves to an existing tag — so a green `main` already covers it. Let those jobs run as a backstop; don't serialize behind them. (When the mngr branch *also* carries mngr/minds code, its CI is real signal — gate on it.)

**So don't run the steps strictly in series.** Once `main` is green and the bump commit exists, the release SHA (`GREEN_MNGR_SHA` = mngr release-branch HEAD) is fixed: cut the FCT branch (step 3) and fire launch-to-msg (step 4) right away, and let both branches' traditional CI finish in parallel. The numbering below is dependency order, not "wait for each."

## Procedure

### 1. Bump version + FALLBACK_BRANCH (mngr branch)

For an iteration of the same version, skip. To bump: set `apps/minds/package.json` `version` (e.g. `0.3.1`) and `templates.py` `FALLBACK_BRANCH` to `"minds-v0.3.1"`. This bakes in a tag that doesn't exist until step 7 — fine, because step 4 overrides the FCT ref via `template_ref`, so the tag is only hit in step 8.

### 2. Traditional CI on both branches (parallel, not a serial gate)

`ci.yml` runs only on PRs (any branch) and on push to `main` — **a bare branch push triggers nothing**, so open a branch as a PR when you want its CI. Gate on the **FCT** PR's `test` job (`uv sync --all-packages` + root/`system_interface` pytest — exactly what a bad vendor refresh trips). The **mngr** branch's suites (`test-offload`, `test-docker`, `test-docker-electron`, `test-offload-acceptance`) are real signal only if it carries mngr/minds code; for a version-bump-only branch they're redundant with a green `main` (see "What actually gates a release"), so a PR there is optional. The release SHA — `GREEN_MNGR_SHA` — is the mngr release-branch HEAD (`main` + the bump commit) and doesn't depend on any of this finishing.

### 3. Refresh FCT `vendor/mngr` from the green mngr SHA (FCT branch)

On the FCT release branch (cut from `origin/main`, clean tree), with the **mngr checkout positioned at `GREEN_MNGR_SHA`** (the mngr release-branch HEAD), run the sync recipe. You can do this the moment the bump commit exists — no need to wait for step 2's CI.

`just sync-vendor-mngr` reads `FCT_DIR` from your `apps/minds/.env` (Session setup) — no path is baked into the justfile. It does `git archive HEAD` → FCT `vendor/mngr` (tracked files only; keep `apps/minds/`), commits `Sync vendor/mngr to <branch> (<short>)`, aborts if FCT is dirty, and **does not push** — it prints the exact `cd … && git push` line (with the resolved FCT path) for you to run. For why releases use `git archive` (vs the dev loop's `rsync`), see `apps/minds/docs/vendor-mngr-sync.md`.

```bash
just sync-vendor-mngr                       # reads FCT_DIR from .env
# (or pass the path explicitly: just sync-vendor-mngr /abs/path/to/forever-claude-template)
# then copy the `To publish: (cd <fct> && git push origin <branch>)` line the recipe
# printed (it already has the resolved absolute path) and run it verbatim
```

If the new vendor changes an mngr API a consumer calls (e.g. `system_interface`), fix that consumer in this same branch (its own commit, so it stays reviewable).

### 4. Prove the pair green pre-merge

This is the long pole — fire it as soon as the FCT branch exists, in parallel with both branches' traditional CI. The tag doesn't exist yet, so pass the FCT release branch as `template_ref`. `commit_sha` and that branch's `vendor/mngr` must be the same mngr SHA.

```bash
GREEN_MNGR_SHA=<mngr release-branch HEAD: main + the bump commit>   # carried through to steps 6-8
cd "$MNGR"
gh workflow run minds-launch-to-msg.yml -R imbue-ai/mngr \
  -r <mngr-release-branch> -f commit_sha="$GREEN_MNGR_SHA" -f template_ref=<fct-release-branch>
```

`build` packages/reuses (keyed by `commit_sha`) the bundle; `launch_to_msg` launches it, creates an agent from the FCT ref, sends a first message, asserts the round-trip. Invoke from the mngr cwd — from the FCT cwd it has 404'd mid-create and duplicated the run.

### 5. Review real code only (if any)

The version bump and the `vendor/mngr` refresh need no review (see "The two release branches"). The only thing to read is reviewable code that rode along — mngr/minds code on the mngr branch, or a `system_interface` fix on the FCT branch. With `main` unprotected, even that review is social, not a gate. Nothing is tagged yet.

### 6. Land both branches on `main`

With `main` unprotected you can merge locally (`git merge --no-ff <branch>`, then push) or via a PR — either works. **Land the mngr branch with a merge commit, never a squash.** `main` can advance past the SHA you built and verified in step 4 (`$GREEN_MNGR_SHA`) while you were verifying; a merge commit keeps that exact SHA reachable on `main` as a parent (a squash replaces it with a new commit whose tree also contains the drift — and the binary you verified was built from neither).

The tag pins **`$GREEN_MNGR_SHA`** — the SHA the binary was built from and FCT's `vendor/mngr` was archived from — **not** `main`'s HEAD. Confirm the *commit you'll actually tag* (FCT `origin/main` post-merge, not your local working copy) still matches that SHA:

Compare the two git **trees** by `(blob-hash, path)` — content-exact, and immune to the symlinks, file modes, and `.gitignore` drops that make `diff -r` on extracted tarballs noisy. The only expected delta is files FCT's `**/.minds/` ignore strips on `git add` (Vault policies + deploy scripts — not part of the installed mngr package); **anything else, especially under `vendor/mngr/libs/**`, is a real mismatch.**

```bash
GREEN_MNGR_SHA=<the SHA from step 4>
git -C "$FCT" fetch origin --quiet
real_diff=$(diff \
  <(git -C "$MNGR" ls-tree -r "$GREEN_MNGR_SHA"        | awk '{print $3, $4}' | sort) \
  <(git -C "$FCT"  ls-tree -r origin/main:vendor/mngr  | awk '{print $3, $4}' | sort) \
  | grep '^[<>]' | grep -v '\.minds/')
[ -z "$real_diff" ] \
  && echo "OK: vendor/mngr == mngr $GREEN_MNGR_SHA (modulo .minds/)" \
  || { echo "MISMATCH — re-run step 3 / re-merge FCT:"; echo "$real_diff"; }
```

Comparing the mngr side against `main` (HEAD) instead of `$GREEN_MNGR_SHA` may surface extra differences — that's **expected drift** (unrelated commits landed on mngr `main` after you built), not an error. Always compare against, and tag, `$GREEN_MNGR_SHA`.

### 7. Tag the verified pair — *not* `main` HEAD

Tag mngr at **`$GREEN_MNGR_SHA`** (the built+verified SHA; reachable on `main` as the merge parent) and FCT at the commit whose `vendor/mngr` is that SHA's archive (the FCT branch's merge into `main`):

```bash
# $GH_TOKEN, $MNGR, $FCT from Session setup
VERSION=minds-v0.3.1
GREEN_MNGR_SHA=<the SHA from step 4>
git -C "$FCT" fetch origin --quiet; FCT_SHA=$(git -C "$FCT" rev-parse origin/main)   # vendor/mngr == archive $GREEN_MNGR_SHA (verified in step 6)

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
