Two changes supporting the new macOS smoke-test CI job:

- `integ_check.py` gains a `--launch-only` flag (also `MINDS_INTEG_LAUNCH_ONLY=1`):
  it stops once the create form renders, without submitting it or creating an
  agent. For environments with no Lima VM / Docker to host an agent.
- Fix `pnpm install` failing with `ERR_PNPM_IGNORED_BUILDS`: the `allowBuilds`
  list in `pnpm-workspace.yaml` was stale and did not account for the
  `@firebase/util`, `dtrace-provider`, and `protobufjs` transitive deps, which
  carry install scripts. They are now listed (as not-built), so `pnpm install`
  -- and the ToDesktop packaging step -- no longer exit non-zero.
