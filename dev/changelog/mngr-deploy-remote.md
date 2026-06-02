Fixed stale references in the `minds-dev-workflow` skill and the `minds-start`
justfile error hints:

- Dev env naming corrected from `<your-user>-dev` to `dev-<your-user>`. The
  `DevEnvName` validator requires the tier prefix first (`dev-`/`ci-`), so
  `josh-dev` is invalid while `dev-josh` is valid. Also corrected the derived
  paths the skill documented (`MINDS_ROOT_NAME=minds-dev-<user>`, env root
  `~/.minds-dev-<user>/`, container `minds-dev-<user>-mindtest-host`).
- Worktree base branch example `josh/start-minds` (no longer exists on the FCT
  remote) replaced with `origin/main` in the skill and in both `just
  minds-start` error hints.
- Pool-host baking described as OVH-backed (the imbue_cloud pool's VPS provider)
  rather than the outdated "Vultr".
