## GCP provider support: root-level changes

- Top-level coverage configuration adds `--cov=imbue.mngr_gcp` so the new package contributes coverage data.
- `scripts/make_cli_docs.py` adds `gcp` to `SECONDARY_COMMANDS` so the `mngr gcp` operator command group gets generated docs (required by `help_formatter_test`).
- `uv.lock` updated to add the new `imbue-mngr-gcp` workspace package and its dependencies (`google-cloud-compute`, `google-auth`, and their transitive deps).

- `.mngr/settings.toml` gains a `gcp` create-template (`mngr create -t gcp`) and a shared `[providers.gcp]` block, the analogue of the existing `modal` template. Like the `aws` template it builds via the `mngr_vps_docker` backend (`--file=` + `.` context) on depot's remote builders (`builder = "DEPOT"`), so it needs `DEPOT_TOKEN` and `GH_TOKEN` at create time. The provider defaults to `us-west1`/`us-west1-a` on an `e2-standard-2` VM; per-developer `allowed_ssh_cidrs` stays in the gitignored `.mngr/settings.local.toml` and the SSH firewall is created once via `mngr gcp prepare`.
