## Vultr provider

- The Vultr release-test settings now also disable the `azure` provider (`[providers.azure] is_enabled = false`), mirroring the existing modal/gcp/aws/ovh disables. Without it, `mngr list` inside the Vultr lifecycle tests would enumerate the newly-added azure provider and exit non-zero when Azure credentials weren't resolvable in that subprocess, failing the Vultr tests for a non-Vultr reason.
