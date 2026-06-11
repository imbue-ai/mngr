## Azure provider registration

- Added `azure` to the set of remote provider backends that are skipped when tests load local-only backends (`_REMOTE_BACKEND_NAMES` in `providers/registry.py`), so the new `mngr_azure` plugin behaves like `aws` / `gcp` / `vultr` during test isolation.
