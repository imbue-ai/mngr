Hardened the e2e tutorial list tests so a bare `mngr list` (full provider discovery) behaves deterministically regardless of which provider plugins are installed.

The e2e fixture now pins `enabled_backends` to the providers the suite actually exercises (local, ssh, modal, and docker when a Docker daemon is reachable). Previously, because the monorepo installs every provider plugin, full-discovery list commands also probed aws/azure/gcp/vultr/imbue_cloud; without credentials those backends either probed slowly (pushing discovery past the command timeout) or surfaced a provider-unavailable error that made `mngr list` exit non-zero, breaking the list tests that expect a clean, empty listing.

Also added an explicit `@pytest.mark.timeout(60)` to `test_list_label_filter`, since its full-discovery `mngr list` routinely runs longer than the default 10s per-test timeout.

Added `test_list_label_filter_discriminates`, a populated happy-path test that creates two agents with different `TEAM` labels and asserts the `mngr list --label TEAM=backend` shorthand keeps only the matching agent.
