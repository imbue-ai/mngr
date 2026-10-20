Record which `default-workspace-template` tree a minds snapshot e2e run built its
workspace from, and report it on the job that fails.

The snapshot lane clones the template branch paired by name with the mngr
branch, shallowly, from a branch that keeps moving -- so the branch name alone
never identified the tree a failure actually ran against, and the name was only
logged into the build job, not the test job that goes red.
`materialize_paired_default_workspace_template_worktree` now returns that tree's
ref and the commit it resolved to alongside its path, and
`snapshot_minds_e2e_state.py --template-coordinate-output <file>` writes the
pair out for CI to forward into the `Minds Snapshot Resume Tests` check-run
summary.

Nothing about which template a run builds from has changed; it is now recorded.
A later CI flake sweep can tell whether two failures on two mngr branches
reproduced each other or ran against two different authors' unmerged template
branches.
