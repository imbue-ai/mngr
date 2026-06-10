# e2e: detect the CI branch so the FCT branch-matching step fires

The Electron e2e workspace runner pairs the current mngr branch with a
same-named forever-claude-template branch (`resolve_fct_path` step 2), falling
back to FCT `main` otherwise. In CI the checkout is a detached HEAD, so
`git rev-parse --abbrev-ref HEAD` returned `HEAD` and the branch-matching step
never fired -- a PR that changes the mngr<->FCT config contract could only ever
be tested against FCT `main`. `_current_mngr_branch` now consults GitHub
Actions' `GITHUB_HEAD_REF` (PR source branch) / `GITHUB_REF_NAME` (push branch,
ignoring `<n>/merge` refs) before the git fallback, so the FCT branch matching
works in CI. Other PRs are unaffected (they have no matching FCT branch and
still use FCT `main`).
