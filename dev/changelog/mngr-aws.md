## AWS provider support: root-level changes

- `mngr create` CLI markdown docs regenerated to include the new AWS provider's build-args help (removes the dropped Vultr/OVH `--vps-os=` line at the same time).
- Top-level coverage configuration adds `--cov=imbue.mngr_aws` so the new package contributes coverage data.
- `uv.lock` reverted to match `main` except for the new AWS additions (`imbue-mngr-aws`, `boto3-stubs`, `botocore-stubs`, `mypy-boto3-ec2`, `types-awscrt`, `types-s3transfer`). An earlier full re-lock had floated ~100 unrelated packages to latest, including `ty` 0.0.24 -> 0.0.39, whose stricter checks surfaced 52 pre-existing type errors repo-wide and failed CI.
