## Azure provider registration

- Added `azure` to the set of remote provider backends that are skipped when tests load local-only backends (`_REMOTE_BACKEND_NAMES` in `providers/registry.py`), so the new `mngr_azure` plugin behaves like `aws` / `gcp` / `vultr` during test isolation.

- `ProviderUnavailableError` now accepts an optional `user_help_text` override. The default still tells the user to start Docker / disable the provider, but cloud providers (whose "unavailable" cause is a credential/subscription problem, not a local daemon) can pass curated guidance instead -- so a cloud auth failure no longer advises "start Docker". Used by the Azure provider.

- Regenerated `mngr azure` and `mngr ovh` CLI docs: `mngr azure prepare` / `mngr azure cleanup` and `mngr ovh list` now take a `--provider` option (and the standard common options) so they read defaults from the selected `[providers.NAME]` settings.toml block.

- Added the `azure` provider backend (`imbue-mngr-azure`) to the install-wizard plugin catalog (`PLUGIN_CATALOG`), so `mngr plugin install` offers it alongside `aws` / `gcp` / `ovh` / `vultr`.
