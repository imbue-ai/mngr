Made the e2e tutorial test fixture's provider setup deterministic regardless of the host's cloud CLIs, credentials, or Docker availability:

- The credential-only cloud providers (aws, azure, gcp, vultr, ovh, imbue_cloud) are now disabled in the e2e fixture. Their backend plugins are installed in the dev checkout, so a read-only `mngr list` would otherwise instantiate each default provider and exit non-zero (provider-inaccessible) whenever the host happened to have that CLI on PATH but no credentials configured.

- The Docker provider is now enabled only for tests that declare they need it (`@pytest.mark.docker` / `@pytest.mark.docker_sdk`). Non-docker tests disable it so they no longer require a reachable Docker daemon, restoring the docker marker's contract of letting Docker-less environments skip only the docker tests.
