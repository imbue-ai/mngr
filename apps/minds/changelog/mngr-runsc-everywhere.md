Fixed the dev create-form defaults so they work on any tier, including staging
and production. The `MINDS_WORKSPACE_GIT_URL` / `_NAME` / `_BRANCH` env vars
(which point the create form at the operator's local FCT worktree) were
previously honored only on per-developer dev tiers and silently dropped on the
shared `minds` / `minds-staging` tiers -- so `just minds-start` against staging
fell back to the public GitHub FCT on `main`, and local FCT changes could never
be tested there.

The tier-based gate is replaced with an explicit opt-in: the form honors those
vars only when `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1` is set in the same
environment. `just minds-start` and the e2e workspace runner set it; a normal
end-user `minds run` never does, so a stray `MINDS_WORKSPACE_*` left in the
operator's shell is ignored on every tier (the safety the tier gate provided,
now applied uniformly -- and dev tiers no longer honor stray vars by tier alone).
These defaults point at a local path + dev branch and only make sense for
local-compute launch modes (Lima / Docker), not IMBUE_CLOUD pool leases.
