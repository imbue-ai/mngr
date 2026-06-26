Release minds v0.3.4: bump `apps/minds/package.json` to `0.3.4` and point the shipped binary's `FALLBACK_BRANCH` at the `minds-v0.3.4` forever-claude-template tag. This rolls up all mngr/minds changes that landed on `main` since `minds-v0.3.3`.

Also overhauled the release runbook (`apps/minds/docs/release.md`):

- Reframed the release as two **branches**, not "two PRs": neither `main` is branch-protected, so a PR is never a merge gate. A PR's only role is as a CI surface, because `ci.yml` runs on PRs and on push to `main` but **not on a bare branch push**. The FCT `vendor/mngr` refresh is opened as a PR purely to run its `test` job (it is generated and reproduction-verified, not reviewed).

- Named the actual critical path: the `minds-launch-to-msg` end-to-end run is the only verification `main` never does and is the wall-clock long pole, so fire it as early as the FCT branch exists; traditional CI on a version-bump-only mngr branch is redundant with a green `main` and should not be serialized against.

- Replaced the unreliable `diff -r` vendor-match check with a content-exact `git ls-tree` blob-hash comparison of the FCT vendor tree against the tagged mngr SHA's tree. The only expected delta is the files FCT's `**/.minds/` gitignore strips (Vault policies + deploy scripts, not part of the installed mngr package).
