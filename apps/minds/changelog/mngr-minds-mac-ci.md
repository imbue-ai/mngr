Fix `pnpm install` failing with `ERR_PNPM_IGNORED_BUILDS`: the
`allowBuilds` list in `apps/minds/pnpm-workspace.yaml` was stale and did
not account for the `@firebase/util`, `dtrace-provider`, and `protobufjs`
transitive deps, which carry install scripts. They are now listed (as
not-built), so `pnpm install` -- and the ToDesktop packaging step -- no
longer exit non-zero.
