## AWS provider

- The AWS release-test settings now also disable the `azure` provider (`[providers.azure] is_enabled = false`), mirroring the existing modal/gcp/vultr/ovh disables. Without it, `mngr list` inside the AWS lifecycle tests would enumerate the newly-added azure provider and exit non-zero when Azure credentials weren't resolvable in that subprocess, failing the AWS tests for a non-AWS reason (the same gap that was already fixed for gcp).

- `mngr aws prepare` / `mngr aws cleanup` group their AWS-specific options under a "Provider" option group, so `--help` and the generated docs list them ahead of the shared common options instead of below them.
