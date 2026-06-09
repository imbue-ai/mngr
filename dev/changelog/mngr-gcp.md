## GCP provider support: root-level changes

- Top-level coverage configuration adds `--cov=imbue.mngr_gcp` so the new package contributes coverage data.
- `uv.lock` updated to add the new `imbue-mngr-gcp` workspace package and its dependencies (`google-cloud-compute`, `google-auth`, and their transitive deps).
