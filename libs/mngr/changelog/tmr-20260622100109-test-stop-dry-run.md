Fix the `test_stop_dry_run` e2e release test (covers the `mngr list --ids | mngr stop - --dry-run` tutorial block) so it passes.

The test had no `@pytest.mark.timeout`, so the default 10s pytest timeout fired during `mngr create`. It now uses `@pytest.mark.timeout(240)`, and the two provider-enumerating commands get explicit per-command timeouts (`mngr list --ids | mngr stop - --dry-run` enumerates every provider, including Modal, twice -- once in `list` and again in `stop -`, which resolves the piped ids via `find_all_agents`).

It also dropped the superfluous `@pytest.mark.modal` and `@pytest.mark.rsync` marks: the test only creates a local command agent and enumerates providers, reaching Modal solely through the in-process gRPC SDK inside the mngr subprocess (which the resource guard cannot observe) and never invoking the rsync binary. This matches the sibling `test_start_dry_run`, which performs the same enumeration without those marks.
