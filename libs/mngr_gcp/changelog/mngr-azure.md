## GCP provider

- The GCP release-test settings now also disable the `azure` provider (`[providers.azure] is_enabled = false`), mirroring the existing modal/aws/vultr/ovh disables. Without it, `mngr list` inside the GCP lifecycle tests would enumerate the newly-added azure provider and exit non-zero when Azure credentials weren't resolvable in that subprocess, failing the GCP tests for a non-GCP reason.

- `mngr gcp prepare` / `mngr gcp cleanup` group their GCP-specific options under a "Provider" option group, so `--help` and the generated docs list them ahead of the shared common options instead of below them.
