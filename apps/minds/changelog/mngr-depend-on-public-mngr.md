default-workspace-template installs mngr from the public mirror at a pinned commit instead of
vendoring this monorepo, so a workspace built anywhere -- the dev desktop app, a pool bake, the
paired-branch CI harness -- runs the mngr the template pins, never a local checkout.
`just minds-start` no longer copies this checkout into the template worktree, and
`apps/minds/scripts/propagate_changes` syncs only the template into a running agent. The
paired-branch CI worktree (`default_workspace_template_worktree.py`) clones the template branch and
builds against its pin. Getting an mngr change into workspaces is: land on `main`, wait for the
mirror, `just dwt-mngr-pin`.

`propagate_changes` is now the only sync needing GNU rsync's `--filter=':- .gitignore'`, so it
carries the openrsync fail-fast check (with the `brew install rsync` fix) that `just minds-start`
used to run before every launch.

The desktop client `propagate_changes` restarts now carries the same `MINDS_WORKSPACE_*` env as
`just minds-start` (branch and the local-defaults opt-in, not just the git URL), so the restarted
client's create form points at the template worktree instead of the released `FALLBACK_BRANCH` tag.
The branch comes from an exported `MINDS_WORKSPACE_BRANCH` when there is one, else from the
worktree itself; a worktree whose branch cannot be read (detached HEAD, or a path that is not a git
repo) is reported with both remedies rather than restarting the client against nothing.

Docs (`docs/dwt-mngr-pin.md`, renamed from `vendor-mngr-sync.md`, `docs/deploy/ops/app-release.md`, `docs/deploy/ops/pool-hosts.md`,
`docs/dev-setup.md`, `docs/embed-contract.md`, `docs/wsl.md`) describe the pin; the release runbook's
vendor-match check becomes a pin-match check. Its step 3 now owns landing the mngr branch by
fast-forward, since nothing can be pinned until the mirror has exported that SHA, and step 6 lands
only the template.
