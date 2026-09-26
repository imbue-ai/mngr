# How default-workspace-template gets its mngr

`default-workspace-template` (DEFAULT_WORKSPACE_TEMPLATE) does not carry a copy of mngr.
It installs `imbue-mngr` and the plugins its `system/config/mngr_plugins.toml` lists
as Python packages from the one commit its root `pyproject.toml` pins under
`[tool.uv.sources]`:

```toml
imbue-mngr = { git = "https://github.com/imbue-ai/mngr", rev = "<commit>", subdirectory = "libs/mngr" }
```

Its `system/scripts/build_workspace.sh` derives everything from that entry --
the `mngr` uv tool and its plugins (`system/scripts/install_mngr.py`), the app tools,
the workspace venv (`uv sync`), and the few non-Python files it fetches from that
commit into `system/vendor/mngr-assets/`. So the pin *is* the mngr that runs inside
every agent, wherever the workspace is built: a release, a pool bake, the dev
desktop app, or a CI harness. Moving the pin is the only way to change it.

The pin names one of two repos:

- the **public mirror**, https://github.com/imbue-ai/mngr, which template `main` and
  every release pin: a commit on the mirror's `main`, or, for a paired change that
  has not merged yet, a public export of the mngr branch on the mirror's
  `export/<branch>` ref;
- the **private repo**, https://github.com/imbue-ai/mngr-internal, which only a
  template branch may pin while a paired change is being iterated on.

Design and invariants: `specs/internal-mngr-pin/spec.md`.

## Iterating on a paired change

From this checkout, with the paired template worktree at
`.external_worktrees/default-workspace-template`:

```bash
git push                      # the pinned commit must be on origin
just dwt-mngr-pin-internal    # pin the template worktree to mngr-internal@HEAD
just minds-start              # or propagate-changes, mngr create, pool-bake-from-worktree
```

`just dwt-mngr-pin-internal` refuses a dirty template worktree and an unpushed HEAD,
has the template's own pin writer (`system/scripts/set_mngr_pin.py`) rewrite every
mngr source to the private repo at HEAD and add the BuildKit lines the credential
rides on, relocks, and commits. The relock, and every build of the pinned template,
needs a read-only credential for the private repo: `MNGR_INTERNAL_GIT_TOKEN` in the
environment (a fine-grained token with Contents: read on mngr-internal), else the
shared one in Vault at `secrets/minds/dev/mngr-internal-git` read with your own
`vault login -method=oidc`. The desktop app (whose Vault fallback needs
`MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1`), the pool bake, and the recipes resolve it
the same way, read the pin off the template's `pyproject.toml`, and refuse before
building when the pin is internal and nothing resolves. In this repo's CI the job's
own token is the credential.

Inside a build the token is one file, `/run/secrets/mngr_internal_git_token`: a
BuildKit secret on docker builds (mngr_vps forwards it to an outer box), an uploaded
file that the `lima` and `modal` templates delete right after the workspace build.
Only the commands that fetch mngr run with it. A public pin builds with no credential
and no BuildKit: the `--secret` arguments and Dockerfile mounts exist only while the
pin is internal, which is why an internal-pin branch needs `docker buildx` locally.

Three consequences of an internal pin:

- the template's own public CI is red (it holds no credential, by design, and fails
  up front saying so), and its `main` refuses the merge: the `pin-is-public` check is
  required there;
- in-workspace reinstalls of the mngr tool (`update-self`, `mngr plugin add`) have no
  credential and fail; a workspace built from an internal pin is rebuilt, not updated;
- before the template branch merges, the pin has to move to a public commit.

## Making the pair mergeable

```bash
git push                      # the exported commit must be on origin
just dwt-mngr-pin-export      # export HEAD's public subset, pin the template to it
```

`just dwt-mngr-pin-export` dispatches `mirror-push.yml`'s export-branch run on the
current branch, which squashes HEAD's public subset onto the mirror's
`export/<branch>` ref (parent: public `main`'s tip; never `main` itself), waits for
it, checks the resulting commit's `GitOrigin-RevId` is HEAD or an ancestor of it (the
newest commit that touched a public path), and pins the template to
that public commit, dropping the BuildKit lines. The template PR's CI goes green with
no credential. Merge the template PR first, then the mngr PR: template `main` pinned
to an export is left alone by the twice-daily bump until the export's mngr commit is
on mngr `main`, and moved to the mirror of `main` on the next run after that. Re-run
the recipe whenever the mngr branch changes before it merges.

## Getting an mngr change into workspaces

1. Land it on mngr `main`.
2. Wait for `mirror-push.yml` to succeed for that push.
3. `just dwt-mngr-pin [default-workspace-template-path]` (`private.just`)
   resolves mngr `HEAD` to its mirror commit, rewrites every pin in
   DEFAULT_WORKSPACE_TEMPLATE (an internal or export pin included), runs `uv lock`
   there, and commits. Position the mngr checkout at the exact commit you want first.
4. Open the DEFAULT_WORKSPACE_TEMPLATE PR; its CI builds against the new pin.

Internal commits are not on the mirror; each mirrored commit carries a
`GitOrigin-RevId` trailer naming the internal commit it was exported from, and an
internal commit that touched only private paths has no mirror commit of its own --
its public subset is the tree of the newest export whose internal commit it descends
from. `scripts/bump_dwt_mngr_pin.py` does that resolution (`--mngr-sha <sha>` alone
prints the mirror commit; add `--template <dir>` to rewrite and relock; `--read-pin
--template <dir>` prints the current pin's kind and commit; `--pin-origin-rev` the
mngr-internal commit it was exported from). The mirror
exports on each push to mngr `main` that moved a public path, so only a commit that
reached `main` as a push tip has an export of its own; the resolver refuses a SHA that
is not on `main`'s first-parent chain -- an unmerged branch commit, or a branch head
that reached `main` through a merge commit: for either, the resolution would land on an
ancestor's export and silently leave the commit's own public changes out. Land a
release SHA by fast-forward, or pin the merge commit that landed it; an unmerged
branch gets an export of its own instead (`just dwt-mngr-pin-export`).
`mirror-push.yml` runs on every push to `main`; resolve only after it has succeeded
for the SHA, or the pin lands on a stale tree.

The `bump_dwt_mngr_pin` job in `.github/workflows/minds-launch-to-msg.yml` does the same
bump on the unattended main-vs-main run, waiting for `mirror-push.yml` first and
pushing the result to DEFAULT_WORKSPACE_TEMPLATE `main`, so the pair that run
verifies is the pair on both mains. The job moves the pin only if mngr `main` contains
the mngr-internal commit the pin was exported from (an export of an unmerged branch is
left alone, since rewriting would drop the branch's changes) and says so in its summary
and the Slack message. A dispatch that names a `commit_sha` or `template_ref` never
moves a pin.

The release procedure -- including the pin-match invariant (DEFAULT_WORKSPACE_TEMPLATE
must pin the mirror commit of the exact mngr SHA it is tagged with, so neither an
internal pin nor an export can be tagged) -- is in `apps/minds/docs/deploy/ops/app-release.md`.

## What the paired-branch harnesses test

`materialize_paired_default_workspace_template_worktree`
(`apps/minds/imbue/minds/desktop_client/default_workspace_template_worktree.py`) gives
the snapshot bake, the full-flow e2e and `just minds-test-electron*` a throwaway
clone of the DEFAULT_WORKSPACE_TEMPLATE branch named like the current mngr branch
(else `main`). The workspace it builds runs the mngr that clone pins. With the paired
branch pinned to this branch's commit on mngr-internal, or to its public export, that
is this checkout's mngr inside a workspace, verified on the PR; with a mirror-of-main
pin, it is the released mngr and the run verifies this checkout's *minds app* against
a workspace.

## `system/vendor/tk`

`system/vendor/tk/` is a forked-and-modified copy of the
[tk](https://github.com/wedow/ticket) ticket tracker. We maintain it by hand and
upgrade it manually; we do not pull from upstream. It is a plain snapshot -- not
a subtree or submodule.
