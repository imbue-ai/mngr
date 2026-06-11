## Azure provider wiring

- Added `--cov=imbue.mngr_azure` to the root pytest coverage config so the new `mngr_azure` package is covered alongside the other provider plugins. The package is picked up automatically by the `libs/*` uv workspace glob.
