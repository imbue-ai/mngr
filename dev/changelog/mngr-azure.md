## Azure provider wiring

- Added `--cov=imbue.mngr_azure` to the root pytest coverage config so the new `mngr_azure` package is covered alongside the other provider plugins. The package is picked up automatically by the `libs/*` uv workspace glob.

- Registered the `azure` command group in `scripts/make_cli_docs.py` (`SECONDARY_COMMANDS`) so `mngr azure` gets a generated doc page, alongside `aws` / `gcp`.
