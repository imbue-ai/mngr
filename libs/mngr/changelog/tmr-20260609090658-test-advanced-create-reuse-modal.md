Fixed: the very first `mngr create --provider modal NAME` (including the
`--reuse` form) against a brand-new per-user Modal environment no longer fails
with `Provider 'modal' has no state yet`. The CLI create path resolved the
new-host provider (used to tear the host down if a post-create step fails) with
read-only semantics, so on a not-yet-existing Modal environment it raised
`ProviderEmptyError` before the create could bootstrap the environment. It now
resolves that provider with `is_for_host_creation=True`, matching the API-layer
resolution, so the environment is created on first use as documented.
