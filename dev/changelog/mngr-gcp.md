## GCP provider support: root-level changes

- Top-level coverage configuration adds `--cov=imbue.mngr_gcp` so the new package contributes coverage data.
- `scripts/make_cli_docs.py` adds `gcp` to `SECONDARY_COMMANDS` so the `mngr gcp` operator command group gets generated docs (required by `help_formatter_test`).
- `uv.lock` updated to add the new `imbue-mngr-gcp` workspace package and its dependencies (`google-cloud-compute`, `google-auth`, and their transitive deps).
