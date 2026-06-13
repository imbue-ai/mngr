Add a SMOLVM launch mode: workspaces can now run in smolvm microVMs (libkrun; KVM on Linux, Hypervisor.framework on macOS) with sub-second VM boots.

The mode uses the same FCT docker image as docker mode (built via the smolvm provider's --dockerfile pipeline and cached by image id) and backs /mngr with a btrfs data disk, so host_backup's btrfs_local snapshots work without any privileged steps. Requires a smolvm build with data-disk support on PATH; the provider's capability check guards hosts without one.

smolvm workspaces are shutdown-capable like docker and lima ones: the landing page Start/Stop controls and the quit-time stop prompt apply to them, so an idle-stopped smolvm workspace can be restarted from the UI.

Fix agent creation from a local git worktree (the dev `minds-start` flow, and any local-worktree source): `clone_git_repo` now checks out the fetched ref so the clone has a materialised working tree, matching what `git clone` produces. A recent rewrite that swapped `git clone` for `git init` + `git fetch` (to accept commit SHAs) dropped the checkout, so the worktree-overlay rsync's files landed untracked and the follow-up checkout aborted with "untracked working tree files would be overwritten by checkout", failing the create. This affected docker and lima local-worktree creates too, not just smolvm.
