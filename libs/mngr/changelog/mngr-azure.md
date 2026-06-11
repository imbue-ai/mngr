## Azure provider registration

- Added `azure` to the set of remote provider backends that are skipped when tests load local-only backends (`_REMOTE_BACKEND_NAMES` in `providers/registry.py`), so the new `mngr_azure` plugin behaves like `aws` / `gcp` / `vultr` during test isolation.

- `ProviderUnavailableError` now accepts an optional `user_help_text` override. The default still tells the user to start Docker / disable the provider, but cloud providers (whose "unavailable" cause is a credential/subscription problem, not a local daemon) can pass curated guidance instead -- so a cloud auth failure no longer advises "start Docker". Used by the Azure provider.
