# How default-workspace-template gets its mngr

`default-workspace-template` (DEFAULT_WORKSPACE_TEMPLATE) does not carry a copy of mngr.
It installs `imbue-mngr` and the plugins its `system/config/mngr_plugins.toml` lists
as Python packages from the **public mirror**, https://github.com/imbue-ai/mngr, at
the one commit its root `pyproject.toml` pins under `[tool.uv.sources]`:

```toml
imbue-mngr = { git = "https://github.com/imbue-ai/mngr", rev = "<commit>", subdirectory = "libs/mngr" }
```

Its `system/scripts/build_workspace.sh` derives everything from that entry --
the `mngr` uv tool and its plugins (`system/scripts/install_mngr.py`), the app tools,
the workspace venv (`uv sync`), and the few non-Python files it fetches from that
commit into `system/vendor/mngr-assets/`. So the pin *is* the mngr that runs inside
every agent, wherever the workspace is built: a release, a pool bake, the dev
desktop app, or a CI harness. Moving the pin is the only way to change it.

## Getting an mngr change into workspaces

1. Land it on mngr `main`.
2. Wait for `mirror-push.yml` to succeed for that push.
3. `just dwt-mngr-pin [default-workspace-template-path]` (`private.just`)
   resolves mngr `HEAD` to its mirror commit, rewrites every pin in
   DEFAULT_WORKSPACE_TEMPLATE, runs `uv lock` there, and commits both. Position the
   mngr checkout at the exact commit you want first.
4. Open the DEFAULT_WORKSPACE_TEMPLATE PR; its CI builds against the new pin.

A DEFAULT_WORKSPACE_TEMPLATE change that depends on an mngr change therefore lands
after it, never alongside it: DEFAULT_WORKSPACE_TEMPLATE CI only ever sees mngr at
the pin, and an unmerged mngr branch has no mirror commit to pin.

Internal commits are not on the mirror; each mirrored commit carries a
`GitOrigin-RevId` trailer naming the internal commit it was exported from, and an
internal commit that touched only private paths has no mirror commit of its own --
its public subset is the tree of the newest export whose internal commit it descends
from. `scripts/bump_dwt_mngr_pin.py` does that resolution (`--mngr-sha <sha>` alone
prints the mirror commit; add `--template <dir>` to rewrite and relock). The mirror
exports on each push to mngr `main` that moved a public path, so only a commit that
reached `main` as a push tip has an export of its own; the resolver refuses a SHA that
is not on `main`'s first-parent chain -- an unmerged branch commit, or a branch head
that reached `main` through a merge commit: for either, the resolution would land on an
ancestor's export and silently leave the commit's own public changes out. Land a
release SHA by fast-forward, or pin the merge commit that landed it.
`mirror-push.yml` runs on every push to `main`; resolve only after it has succeeded
for the SHA, or the pin lands on a stale tree.

The `bump_dwt_mngr_pin` job in `.github/workflows/minds-launch-to-msg.yml` does the same
bump on the unattended main-vs-main run, waiting for `mirror-push.yml` first and
pushing the result to DEFAULT_WORKSPACE_TEMPLATE `main`, so the pair that run
verifies is the pair on both mains. A dispatch that names a `commit_sha` or
`template_ref` never moves a pin.

The release procedure -- including the pin-match invariant (DEFAULT_WORKSPACE_TEMPLATE
must pin the mirror commit of the exact mngr SHA it is tagged with) -- is in
`apps/minds/docs/deploy/ops/app-release.md`.

## What the paired-branch harnesses test

`materialize_paired_default_workspace_template_worktree`
(`apps/minds/imbue/minds/desktop_client/default_workspace_template_worktree.py`) gives
the snapshot bake, the full-flow e2e and `just minds-test-electron*` a throwaway
clone of the DEFAULT_WORKSPACE_TEMPLATE branch named like the current mngr branch
(else `main`). The workspace it builds runs the mngr that clone pins, so those runs
verify this checkout's *minds app* against a workspace, not this checkout's mngr
inside one; an mngr change is first exercised inside a workspace by the
main-vs-main launch-to-msg run after `bump_dwt_mngr_pin` moves the pin.

## `system/vendor/tk`

`system/vendor/tk/` is a forked-and-modified copy of the
[tk](https://github.com/wedow/ticket) ticket tracker. We maintain it by hand and
upgrade it manually; we do not pull from upstream. It is a plain snapshot -- not
a subtree or submodule.
