# How `vendor/mngr` is synced

`forever-claude-template` (FCT) vendors a full copy of the mngr monorepo at
`vendor/mngr/`. The FCT Docker build installs the container's `mngr` from that
directory editable (`uv tool install -e vendor/mngr/libs/mngr`, run by
`scripts/build_workspace.sh`), so whatever lands in `vendor/mngr/` *is* the
mngr that runs inside every agent.

`vendor/mngr/` is a plain copied-in snapshot. It is **not** a git subtree and
**not** a git submodule -- never run `git subtree` or `git submodule` against
it. It is refreshed by copying mngr's files in, via one of two mechanisms.

## The two mechanisms

| Mechanism | Form | Carries | Commits in FCT? | Used for |
|---|---|---|---|---|
| **`git archive`** | `git archive HEAD` -> wipe `vendor/mngr/` -> untar | committed/tracked files at an exact SHA; permissions normalized; reproducible | yes | releases |
| **`rsync`** | `rsync -a --delete --filter=':- .gitignore' --exclude=.git --exclude=uv.lock` | the working tree, including uncommitted edits, gitignore-filtered | no | dev iteration and pool bakes |

Use **archive** for a reproducible, committed snapshot tied to an exact mngr SHA
(the release flow). Use **rsync** to get your *uncommitted* local mngr changes
into a container without a commit (the dev loop, and baking a pool host from a
working tree).

## `git archive` -- the release sync

`just sync-vendor-mngr [fct-path]` (root `justfile`) archives mngr `HEAD`,
replaces `vendor/mngr/` with the snapshot, and commits in FCT. It carries only
committed content, so position your mngr checkout at the exact commit you want
to vendor first. The full release procedure -- including the vendor-match
invariant (FCT `vendor/mngr` must be the `git archive` of the exact mngr SHA it
is tagged with) -- is in `apps/minds/docs/release.md`.

## `rsync` -- the dev / bake sync

Every rsync path uses one form:

```
rsync -a --delete --filter=':- .gitignore' --exclude=.git --exclude=uv.lock SRC/ vendor/mngr/
```

- `--filter=':- .gitignore'` is rsync's dir-merge filter: it reads `.gitignore`
  at each level under the source and applies its exclude rules, so
  `__pycache__`, `.venv`, `node_modules`, `.test_output`, `.mypy_cache`,
  `.ruff_cache`, `.pytest_cache`, `.external_worktrees`, etc. are excluded
  without being listed.
- The two manual excludes cover what `.gitignore` deliberately omits: `.git`
  (git's internal dir) and `uv.lock` (committed at the mngr root, but each
  install context regenerates its own).

The exclude set is defined once in code, in
`libs/mngr_imbue_cloud/.../bake/pool_bake.py`
(`_VENDOR_RSYNC_MANUAL_EXCLUDES` and `_GITIGNORE_RSYNC_FILTER`). Three paths
populate `vendor/mngr/` from the monorepo with this form; keep them in step with
those constants:

| Path | Where | Trigger |
|---|---|---|
| `just minds-start` | root `justfile` (inline) | every dev-app startup |
| `sync_mngr_into_template` | `pool_bake.py` (the constants) | `mngr imbue_cloud admin pool create --mngr-source ...` / `minds pool create --mngr-source ...` |
| `propagate_changes` | `apps/minds/scripts/propagate_changes` (`RSYNC_EXCLUDES`) | each dev-loop iteration into a running container |

`propagate_changes` additionally protects `runtime/`, `.mngr/`, and
`.claude/settings.local.json` from deletion when rsyncing into `/code/`.

The desktop client's Create flow performs a *separate* rsync -- the FCT worktree
over a shallow clone into `/code/` -- not a monorepo->`vendor/mngr` sync.

## `vendor/tk`

`vendor/tk/` is a forked-and-modified copy of the
[tk](https://github.com/wedow/ticket) ticket tracker. We maintain it by hand and
upgrade it manually; we do not pull from upstream. Like `vendor/mngr`, it is a
plain snapshot -- not a subtree or submodule.
