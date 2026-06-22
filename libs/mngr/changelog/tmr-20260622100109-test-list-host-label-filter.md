Fixed the `mngr list --host-label` tutorial e2e test (`test_list_host_label_filter`):

- Gave it a 60s per-test timeout, matching the other full-discovery list tests. The `--host-label` filter forces host discovery across every provider (an authenticated Modal lookup plus Docker/Vultr probes), which routinely exceeds the default 10s timeout.

- Restricted the e2e test environment to the provider backends the harness can actually authenticate (`enabled_backends = ["local", "docker", "modal"]`). The monorepo installs every provider plugin, and any installed backend is enabled by default; the credential-requiring cloud backends (aws, azure, gcp, ...) deliberately surface a hard `ProviderUnavailableError` on credential-less discovery, which made `mngr list --host-label` (the only list filter that constructs and queries every enabled provider) exit non-zero in a fresh test environment.
