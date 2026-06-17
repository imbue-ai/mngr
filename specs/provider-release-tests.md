# Common Provider Release Test Suite — Proposal

**Status.** Proposal, forward-looking. Companion to `specs/provider-shape.md` (the prescriptive provider contract). Each release-test "trip" below walks an agent through a sequence of paces that, together, exercise one or more sections of the shape doc.

**Goal.** Today every provider (`mngr_aws`, `mngr_azure`, `mngr_gcp`, `mngr_vultr`, `mngr_ovh`, `mngr_modal`) has its own `test_release_*.py` that fans out 4-6 separate tests, each booting a fresh host. With ~3-5 minute spin-up per host across cloud providers, that's 20-30 minutes per provider per CI gate — and the tests diverge in what they actually check. The proposal is to consolidate into a small set of **multi-step trips that amortize one boot across many assertions**, and to make the trip definitions **shared across providers** via a parametrized harness so the same checks run everywhere the shape doc says they should.

**Non-goal.** This is not a replacement for unit tests. Unit-level tests (build-arg parsing, capability flags, config defaults, credential error classification) belong in `*_test.py`. This proposal covers the release tier only.

---

## Why combine steps?

The example walk the user described — start, message the agent and write a file, stop, restart, check the conversation and file are still there, kill in some sketchy way that strands stuff, gc, verify the stranded resources are cleaned up correctly — is one well-shaped trip. It exercises §1.1, §1.2, §1.3, §1.5, §1.6, §1.7, §1.8, §2.1, §2.2 of `specs/provider-shape.md` in a single boot. That is the model.

Today's `test_release_aws.py` has 4 separate lifecycle tests (`test_provider_lifecycle_create_exec_and_destroy`, `test_provider_lifecycle_create_stop_start_destroy`, `test_provider_stop_host_stops_ec2_instance_and_start_resumes`, `test_provider_idle_watcher_auto_stops_then_resumes`) — each boots its own EC2 instance. Three of those four trips could be folded into one with `--stop-host` and an idle wait both happening to the same long-lived host.

---

## Test harness shape

Provide a shared `libs/mngr_vps/imbue/mngr_vps/testing.py::ProviderReleaseTrips` mixin (or equivalent flat conftest fixture in `libs/mngr_vps/conftest.py`) that:

1. **Parametrizes by provider and isolation mode.** Each provider's `test_release_<name>.py` calls into the shared trips with a provider-specific fixture supplying `(provider_name, mngr_ctx, settings_toml_extras, expected_capabilities)`. Each cloud provider now has two shapes selected by `VpsProviderConfig.isolation` (`libs/mngr_vps/imbue/mngr_vps/config.py`, default `IsolationMode.CONTAINER`; `IsolationMode` in `primitives.py`): the default `CONTAINER` shape (a `DockerRealizer` behind the realizer seam) and the bare `NONE` shape (a `BareRealizer` running the agent directly on the VM). The harness parametrizes over both modes so every applicable trip runs against both realizers — see *Trip 1, parametrized over isolation mode* below for the bare-specific assertions.
2. **Reports skip reason with shape-doc cite.** When a trip step is gated by a capability flag (`supports_shutdown_hosts`, `supports_snapshots`, `supports_volumes`) or a documented provider quirk, the harness calls `pytest.skip(f"shape §X.Y: {provider_name} does not support {capability}")` so the skip reason is greppable.
3. **Records cost-impact assertions.** When a step asserts "compute billing stops", the harness calls the provider's cost-stop probe (e.g. `aws_client.describe_instance_state(id) == "stopped"`) — see *Inconsistency callouts* below for which providers can satisfy this honestly today.
4. **Tags every test-launched cloud resource** with `mngr-pytest-launched=true` and runs in a `MNGR_PROJECT_CONFIG_DIR=<tmpdir>` settings.toml that sets `auto_shutdown_seconds=3600`. Pairs with each provider's existing `pytest_sessionfinish` orphan scanner; **gates Vultr/OVH on adding one** (currently missing).

The trip body itself is provider-agnostic — it speaks only through `mngr` CLI + `provider.<methods>` + the cloud-API probe.

---

## The four trips

Each trip is a numbered sequence. Provider-specific inconsistencies are flagged inline with **[INCONSISTENT]** markers and a follow-on note pointing at the shape-doc section and the cite for current behavior. Trip 1 is additionally parametrized over isolation mode (container vs bare) — see *Trip 1, parametrized over isolation mode*.

### Trip 1 — "Full lifecycle + sketchy kill + GC"

The example walk the user described, expanded to also touch capability-flag honesty and per-provider operator setup. **One boot, ~15 minute wall clock.**

Exercises shape doc §1.1, §1.2, §1.3, §1.4, §1.5, §1.6, §1.7, §2.1, §2.2, §2.3, §3.1, §3.8.

1. **Operator setup (prepare).** `mngr <provider> prepare` (where applicable) → assert exit 0. Run a second time → assert still exit 0 and no resources duplicated (idempotency, shape §1.7).
   - **[INCONSISTENT]** Modal/Lima/Docker/SSH/Vultr have no `prepare` analog (shape §1.7 MAY). Skip with `pytest.skip("shape §1.7: <provider> has no prepare command")`.
2. **Create host.** `mngr create <name> --provider <provider> [provider-specific build args]`. Assert exit 0 and host appears in `mngr list` with `host_state=RUNNING` (shape §1.1, §1.2).
3. **Verify cloud-side resource exists.** Probe the provider's API: instance with `mngr-host-id=<id>` tag is present (shape §3.8). Asserts the tag was actually applied.
   - **[INCONSISTENT]** Modal uses `mngr_host_id` (underscore), everyone else uses `mngr-host-id` (dash). Harness probe must accept both. (Round-2 finding N-16.)
4. **Exec + write file.** `mngr exec <name> 'echo "trip1-marker" > /tmp/trip1-marker.txt'`. Assert exit 0.
5. **Message the agent.** Send a known prompt that the agent will store in its conversation log (the prompt is arbitrary — pick something cheap to verify later via `mngr transcript`).
6. **Plain stop.** `mngr stop <name>` → assert exit 0. Verify `mngr list` still shows host as `RUNNING` (only tmux stopped, shape §1.3). Verify file still present via `mngr exec` (the host kept running).
7. **Stop --stop-host: contract branch.**
   - If `provider.supports_shutdown_hosts is False` (Modal): assert `mngr stop --stop-host` raises `HostShutdownNotSupportedError` (shape §1.4 right-refusal). Continue trip without stop.
   - If `provider.supports_shutdown_hosts is True` (everyone else): `mngr stop --stop-host` → assert exit 0. Verify `mngr list` shows host as `STOPPED`. Probe cloud-side: instance/VM is genuinely powered off (shape §1.4 right-real-stop).
     - **[NOW PASSES]** AWS, GCP, and Azure now override `stop_host` to perform a real machine-level stop, so the cost probe succeeds. AWS `stop_host`/`start_host` (`libs/mngr_aws/imbue/mngr_aws/backend.py`, `AwsVpsClient.stop_instance`/`start_instance`); GCP (`libs/mngr_gcp/imbue/mngr_gcp/backend.py`, `GcpVpsClient.stop_instance`/`start_instance`); Azure deallocates via `AzureProvider.stop_host` → `deallocate_instance` (`libs/mngr_azure/imbue/mngr_azure/backend.py`, `AzureVpsClient.deallocate_instance`) so billing genuinely stops. The base placement-stop split lives in `VpsProvider.stop_host` (`libs/mngr_vps/imbue/mngr_vps/instance.py:1222`), which calls `self._realizer.stop_placement(...)` (docker-stop for container, no-op for bare) and lets the cloud subclass layer on the machine-level stop.
     - **[INCONSISTENT]** Vultr and OVH do NOT override `stop_host`/`start_host` (`libs/mngr_vultr/imbue/mngr_vultr/backend.py`, `libs/mngr_ovh/imbue/mngr_ovh/backend.py`); they inherit the base, which only stops the container — cost probe **WILL FAIL** today. `xfail` with reason `"shape §1.4: <provider> claims supports_shutdown_hosts but only stops container"` until a VM-level stop override lands, then flip to a hard fail.
8. **Start.** `mngr start <name>` → assert exit 0. Verify `mngr list` shows host as `RUNNING`. Run `mngr start <name>` a second time → assert exit 0 and no error (idempotent, shape §1.5).
9. **Persistence check after stop+start.** `mngr exec <name> 'cat /tmp/trip1-marker.txt'` → expect `trip1-marker`. `mngr transcript <name>` → expect the prompt from step 5 (shape §1.5).
10. **Capability-flag honesty: volumes.** `provider.list_volumes()`:
    - If `supports_volumes is False`: expect raise or specific error.
    - If `supports_volumes is True`: expect a non-empty list including this host's mount.
      - **[INCONSISTENT]** AWS/Azure/GCP/Vultr/OVH inherit `supports_volumes=True` (`libs/mngr_vps/imbue/mngr_vps/instance.py:419`) but `list_volumes()` returns `[]` (`instance.py:2172`) and `delete_volume` is a no-op (`:2175`). `xfail` until either the flag is flipped to `False` or the implementation lands. Note that AWS and Azure now have a per-host `get_volume_for_host` via the state bucket (`S3Volume`/`BlobVolume`), but `list_volumes`/`delete_volume` remain unimplemented and the flag was not flipped, so this step still fails.
11. **Capability-flag honesty: snapshots.** Skip if `not provider.supports_snapshots`. `supports_snapshots` is now realizer-derived (`VpsProvider.supports_snapshots = self._realizer.supports_snapshots`, `libs/mngr_vps/imbue/mngr_vps/instance.py:411`): the container shape is `True` (`DockerRealizer.supports_snapshots`), the bare shape is `False` (`BareRealizer.supports_snapshots`), so this step is skipped under `isolation=NONE`. Otherwise: `mngr snapshot create <name>` → assert returns a `SnapshotId`. `mngr snapshot list <name>` → assert it appears.
    - **[INCONSISTENT]** For the container shape on AWS/Azure/GCP/Vultr/OVH/Docker the snapshot is still a `docker commit` (`DockerRealizer.snapshot_placement` → `commit_container`) and will not survive `destroy_host` — see Trip 3 for the survive-destroy check. For Modal, the snapshot is portable and DOES survive destroy.
12. **Sketchy kill.** Out-of-band corrupt the host. Pick the sketchiest mechanism the provider exposes:
    - Cloud providers: call the cloud-API directly to force-terminate the instance/VM (bypasses `mngr destroy`, leaves the on-VPS state volume in place but unreachable).
    - Modal: kill the sandbox via Modal SDK without going through `mngr destroy`.
    - Lima: `limactl delete --force` directly.
    - Docker: `docker rm -f <container>` directly.
13. **Discovery reflects the kill.** `mngr list` → host appears as `CRASHED` (shape §1.2). On AWS/Azure/GCP the offline-host reconstruction should fall back gracefully.
    - **[NOW PASSES]** AWS, Azure, and GCP now keep a force-terminated/stopped host visible. Stopped-host reconstruction was lifted into the shared `OfflineCapableVpsProvider` (`libs/mngr_vps/imbue/mngr_vps/instance.py:2240`). AWS and Azure extend `TagMirrorVpsProvider` (`instance.py:2522`) and additionally reconstruct full records from the state bucket (`S3StateBucket`/`BucketHostStateStore` in `libs/mngr_aws/imbue/mngr_aws/state_bucket.py`; `BlobStateBucket` in `libs/mngr_azure/imbue/mngr_azure/state_bucket.py`); GCP extends `OfflineCapableVpsProvider` directly and mirrors via GCE instance metadata (`libs/mngr_gcp/imbue/mngr_gcp/backend.py`). All three now satisfy §1.2.
    - **[INCONSISTENT]** Vultr and OVH still inherit the plain `VpsProvider` discovery, which **drops anything without a current public IP**, so a force-terminated instance vanishes from `mngr list` entirely. The shape doc says hosts MUST stay visible across all states (§1.2). `xfail` for Vultr/OVH; AWS/Azure/GCP are honest.
14. **`mngr gc` reclaims the orphan.** `mngr gc` → assert exit 0. Assert `mngr list --include-destroyed` shows the host as `DESTROYED` (or absent for the providers that don't persist destroyed records).
15. **Verify backend is clean.** Probe cloud-API: no instance with `mngr-host-id=<id>`, no leaked NIC/IP/EBS volume (shape §1.6). For AWS specifically: per-host KeyPair removed.
    - **[INCONSISTENT]** OVH `destroy_host` is "cancel at expiration", not "destroy now". OVH's verify step needs a separate "VPS will expire on date X" assertion, not "VPS is gone". Trip should `xfail` the immediate-cleanup assertion for OVH and assert the cancellation flag instead.
16. **Cleanup refuses if any resources remain.** Skip if no `cleanup` analog. Otherwise: ensure all test hosts destroyed, then `mngr <provider> cleanup` → exit 0, region clean.
    - Bonus: before step 14, run `mngr <provider> cleanup` and assert it **refuses** with a `mngr destroy <agent>` pointer (shape §1.7 MUST).

### Trip 1b — "Second agent on the same host" (piggy-backs on Trip 1)

Inserted between Trip 1 step 9 (persistence check) and Trip 1 step 11 (capability flag checks). **No new boot** — uses the host Trip 1 already provisioned. ~2-3 min wall clock.

Exercises shape doc §1.8 (N agents per host).

1b.1 **Add a second agent.** `mngr exec <host> --new-agent 'echo "trip1b-agent-2" > /tmp/trip1b-agent-2.txt'`. Assert exit 0.
1b.2 **Both agents visible live.** `mngr list` shows two agents under `<host>` with distinct `agent_id`s. Live in-VM/in-container agent discovery is now a realizer method (`HostRealizer.read_live_listing`/`collect_listing_output`/`find_host_record` in `libs/mngr_vps/imbue/mngr_vps/interfaces.py`; container impl in `docker_realizer.py`, bare impl in `bare_realizer.py` reading the root-disk store directly). The provider drives them from `VpsProvider._find_host_record` / its discovery sweep (`libs/mngr_vps/imbue/mngr_vps/instance.py:1680`, `:1693`, `:1838`), so live discovery works uniformly across the VPS family in both isolation modes.
1b.3 **Per-agent persisted records.** Probe `provider.list_persisted_agent_data_for_host(host_id)` → assert length 2 with distinct `id`s.
   - **Per-provider citation.** Modal: `/hosts/{host_id}/{agent_id}.json` on state volume (`libs/mngr_modal/imbue/mngr_modal/instance.py`, `agent_key` at `:756`). AWS/Azure with a state bucket configured: the bucket-backed `BucketHostStateStore` (`libs/mngr_vps/imbue/mngr_vps/host_state_store.py`) under the `state_keys.py` object layout, with the per-field `mngr-agent-<id>-*` tag mirror surviving only as the no-bucket fallback (`TagMirrorVpsProvider._agent_field_tags`, `libs/mngr_vps/imbue/mngr_vps/instance.py:2620`). VPS-family local agent records persist via `VpsHostStore.persist_agent_data` (`libs/mngr_vps/imbue/mngr_vps/host_store.py:187`). The separate local docker provider still uses its own `DockerHostStore.persist_agent_data` (`libs/mngr/imbue/mngr/providers/docker/host_store.py:160`).
   - **[INCONSISTENT — capped]** With a state bucket (AWS/Azure default), per-agent records are full records with no tag cap. Without a bucket the tag-mirror fallback hits the EC2 50-tag wall and drops the `labels` field once it exceeds `_MAX_TAG_VALUE_LEN=256` (`libs/mngr_vps/imbue/mngr_vps/instance.py:2516`, `:2625`). Trip 1b should fork a parametrized variant `trip1b_at_capacity` that loops to N=20 and asserts records survive (bucket path) or the documented drop fires (tag-fallback path). SSH provider has no persisted store at all — Trip 1b runs against SSH but step 1b.3 is `xfail`'d with reason "shape §1.8: SSH has no offline mirror".
1b.4 **Stop-cycle preserves both.** `mngr stop <host> --stop-host` (where supported) → `mngr start <host>` → `mngr exec <host> --agent <agent-2-id> 'cat /tmp/trip1b-agent-2.txt'` returns `trip1b-agent-2`. Also assert agent-1's file from Trip 1 step 4 still present.
1b.5 **Offline mirror shows N agents while VPS is stopped.** After step 1b.4's stop (before the start), `mngr list <host>` → assert two agents still visible.
   - **[NOW PASSES]** Modal, AWS, Azure, and GCP all pass this step today. AWS/Azure read per-agent records from the state bucket via `_offline_agent_dicts_for` → `_state_store` (`libs/mngr_vps/imbue/mngr_vps/instance.py:2555`), falling back to the per-agent tag mirror when no bucket is configured; GCP reconstructs from GCE metadata. The shared offline-discovery hooks live on `OfflineCapableVpsProvider` (`instance.py:2240`).
   - **[INCONSISTENT — high]** Vultr/OVH inherit the plain `VpsProvider`, whose `list_persisted_agent_data_for_host` raises `HostNotFoundError` when the VPS IP is unreachable (`libs/mngr_vps/imbue/mngr_vps/instance.py:2204`-`2207`). `xfail` for those two — the data is intact on the volume but invisible while the VPS is unreachable.
   - SSH `xfail`: no offline view at all (`libs/mngr/imbue/mngr/providers/ssh/instance.py:208`-`216` `to_offline_host` FIXME).
1b.6 **Destroying one agent leaves the other.** `mngr destroy --agent <agent-2-id>` (or whatever the per-agent destroy verb is). Assert agent-1 still listed, its file still readable. Assert `provider.list_persisted_agent_data_for_host` now returns length 1.
   - If no per-agent destroy verb exists in the CLI today, document the gap as a finding and proceed.
1b.7 **Trip 1 continues** at step 11. Trip 1 step 14 (sketchy kill) and 15 (cleanup) will verify both agents go away together when the host is destroyed (shape §1.8 "destroy_host MUST iterate all agents").

### Trip 1, parametrized over isolation mode — container vs bare

Each cloud provider now exposes two shapes via `VpsProviderConfig.isolation`: the default `CONTAINER` (a `DockerRealizer`) and the bare `NONE` (a `BareRealizer` running the agent directly on the VM, no container). The realizer is selected in `VpsProvider._build_realizer` (`libs/mngr_vps/imbue/mngr_vps/instance.py:383`). Bare release tests already exist — fold them into the parametrized harness rather than maintaining a parallel set:

- AWS `test_release_aws.py`: `test_bare_provider_lifecycle_create_exec_and_destroy` (`:583`), `test_bare_provider_stop_host_stops_ec2_instance_and_start_resumes` (`:645`), `test_bare_provider_idle_watcher_auto_stops_then_resumes` (`:705`).
- GCP `test_release_gcp.py`: `test_bare_provider_lifecycle_create_exec_and_destroy` (`:373`).
- Azure `test_release_azure.py`: `test_bare_provider_lifecycle_create_exec_and_destroy` (`:427`).
- Unit coverage: `libs/mngr_vps/imbue/mngr_vps/bare_realizer_test.py`.

The proposal is to run Trip 1 (and Trip 1b and Trip 2) twice — once per `IsolationMode` — wherever the provider fixture declares bare support. Bare differs from container on these steps:

**Bare-support gate (new step, runs before Trip 1 step 1).** Parametrize the provider fixture with the set of isolation modes it supports. `_supports_bare_isolation` defaults `False` on `VpsProvider` (`instance.py:427`) and is overridden `True` only by AWS (`libs/mngr_aws/imbue/mngr_aws/backend.py`), GCP (`libs/mngr_gcp/imbue/mngr_gcp/backend.py`), and Azure (`libs/mngr_azure/imbue/mngr_azure/backend.py`). For Vultr and OVH the `isolation=NONE` parametrization must assert that `mngr create ... ` (with bare config) raises `BareIsolationNotSupportedError` (`libs/mngr_vps/imbue/mngr_vps/errors.py:20`, raised in `VpsProvider.create_host` at `instance.py:682`-`687`) and then stop — there is no bare host to walk through the rest of the trip.

- **Step 11 (snapshots) is skipped under bare.** `BareRealizer.supports_snapshots = False`, so `supports_snapshots` is `False` for the bare shape and Trip 1 step 11 / all of Trip 3 skip cleanly (shape §2.1). `snapshot_placement` raises `SnapshotsNotSupportedError` if called anyway.
- **Step 7 (`--stop-host`) and Trip 2 (idle auto-shutdown) work directly.** Bare powers the VM off itself: `BareRealizer.idle_shutdown_command = "shutdown -P now"` with `idle_shutdown_stops_host = True` (`libs/mngr_vps/imbue/mngr_vps/bare_realizer.py`), so on AWS/GCP that is an instance stop and on Azure the same ARM self-deallocate the container path uses. The cost-stop probe should pass on all three bare-capable clouds (it is already asserted by the AWS `test_bare_provider_stop_host_stops_ec2_instance...` and `..._idle_watcher_auto_stops...` tests).
- **Bare host records carry `None` for container fields.** `BareRealizer.realize_placement` returns an empty `RealizedPlacement()` (`bare_realizer.py`), so `container_name`/`container_id`/`volume_name`/`container_ssh_host_public_key` are `None` (`data_types.py`). Assertions that probe container identity must be container-only.

### Trip 2 — "Idle auto-shutdown contract"

**Goal:** assert `auto_shutdown_seconds` honestly stops billing. **One boot, ~5 minute wall clock** (use shortest acceptable interval).

Exercises shape doc §3.3 (auto-shutdown defaults), §1.4 (cost stop), §2.2 (`supports_shutdown_hosts` honesty under idle).

1. **Create with short auto-shutdown.** `mngr create <name> --provider <provider>`, with settings.toml setting `auto_shutdown_seconds=120` (2 min).
2. **Verify host running.** `mngr list` shows `RUNNING`.
3. **Wait > auto-shutdown.** `sleep 180`.
4. **Verify host stopped/terminated/deleted.** Probe cloud-API for the provider-specific cost-stop state:
   - AWS: instance state `stopped` (with `terminate_on_shutdown=false`) or `terminated` (with `terminate_on_shutdown=true`). Verify EBS still present in former case.
   - GCP: instance state `TERMINATED` (guest `shutdown -P now` lands the instance in `TERMINATED`, no billing; idle watcher wired in `libs/mngr_gcp/imbue/mngr_gcp/backend.py`).
   - Azure: **[NOW PASSES]** VM is genuinely deallocated (billing stops). The idle path runs an ARM self-deallocate via the VM's managed identity + IMDS token rather than an OS poweroff (`_build_self_deallocate_script` / `_install_idle_watcher` in `libs/mngr_azure/imbue/mngr_azure/backend.py`; role via `ensure_self_deallocate_role` / `assign_self_deallocate_role` in `client.py`). Probe: VM power state `deallocated`. Caveat: if the deployment lacks `roleAssignments/write`, Azure logs a warning and only manual `mngr stop` halts billing — assert the deallocated state where the role assignment succeeded.
   - Vultr: **[INCONSISTENT]** OS halts but VPS keeps billing hourly. `xfail`.
   - OVH: **[INCONSISTENT]** Same — OS halt, VPS keeps billing for the month. `xfail`.
   - Modal: sandbox terminated by Modal's own timeout. Probe: Modal client reports sandbox gone.
   - Lima: no `auto_shutdown_seconds` field — skip with `pytest.skip("shape §3.3: lima has no auto-shutdown")`.
   - Docker: no field — skip.
   - SSH: no field — skip.
5. **`mngr start` after auto-shutdown.** If provider supports resume from stopped (AWS, GCP, and Azure are now all honest — GCP restarts the `TERMINATED` instance, Azure restarts the deallocated VM): `mngr start <name>` → assert exit 0. Verify host present and file from a pre-shutdown step is intact.
6. **Destroy.** `mngr destroy <name>` → clean.

### Trip 3 — "Snapshot survives destroy" (snapshot-supporting providers only)

**Goal:** assert that a "snapshot" is actually a snapshot — survives `destroy_host` and can be used by a fresh `mngr create --snapshot <id>`. **One boot + one re-boot, ~10 minute wall clock.**

Exercises shape doc §1.5 (`start_host(snapshot_id=…)`), §1.6 (snapshot MAY survive destroy), §2.1 (`supports_snapshots` honesty).

1. **Skip if `not provider.supports_snapshots`.** SSH/Lima skip; the bare (`isolation=NONE`) shape skips on every cloud provider (`BareRealizer.supports_snapshots = False`); the container shape on everyone else runs.
2. **Create + write file + snapshot.** `mngr create`, `mngr exec <name> 'echo trip3 > /tmp/trip3.txt'`, `mngr snapshot create <name>` → captures `snapshot_id`.
3. **Destroy.** `mngr destroy <name>`.
4. **Verify snapshot record persists.** `mngr snapshot list` (without `<host>`) → assert snapshot still present.
   - **[INCONSISTENT]** Modal preserves snapshot records intentionally (`is_snapshotted_after_create` / portable snapshots in `libs/mngr_modal/imbue/mngr_modal/instance.py`). AWS/Azure/GCP/Vultr/OVH container-shape `docker commit` snapshots (`DockerRealizer.snapshot_placement` → `commit_container`, `libs/mngr_vps/imbue/mngr_vps/docker_realizer.py`) live on the VPS's own disk and die with the VPS — the record vanishes. Trip should `xfail` step 4 for those providers and add an explicit assert "snapshot record absent after destroy" so the test documents what the user gets.
5. **Restore.** `mngr create <new-name> --snapshot <snapshot_id>` → assert exit 0.
   - **[INCONSISTENT]** Only Modal and Docker honor `--snapshot` at create time. The VPS-family base path still silently ignores the parameter: `create_host` accepts `snapshot: SnapshotName | None` (`libs/mngr_vps/imbue/mngr_vps/instance.py:674`) but its body never references it, and `start_host` accepts `snapshot_id: SnapshotId | None` (`instance.py:1277`) but never uses it. Shape §1.5 says either honor or raise — silent no-op is the worst option. `xfail`.
6. **Verify file restored.** `mngr exec <new-name> 'cat /tmp/trip3.txt'` → expect `trip3`.
7. **Cleanup.** `mngr destroy <new-name>`, `mngr snapshot destroy --snapshot <id>`.

### Trip 4 — "Error classification contract"

**Goal:** assert that `mngr list` / `mngr gc` / `mngr create` raise the right error class for each failure mode. **No boot — pure CLI exercise.**

Exercises shape doc §1.2 (ProviderEmpty vs Unavailable), §1.8 (error class for each failure), §3 (Setup) — what the user sees when their credentials are wrong.

For each scenario, run `mngr list` and assert the expected error class **or** the expected silent-skip behavior:

| Scenario | Expected | Inconsistencies |
|---|---|---|
| No `[providers.<X>]` block at all | `ProviderEmptyError` if env-derivable; `ProviderUnavailableError` if creds missing | **[INCONSISTENT]** Vultr (`_credentials_configured` → warn + return `[]`, `libs/mngr_vultr/imbue/mngr_vultr/backend.py`) and OVH (`is_unconfigured` → return `[]`, `libs/mngr_ovh/imbue/mngr_ovh/backend.py`) silently return `[]` instead of raising. Modal raises `ModalAuthError` (a `PluginMngrError`, not the contract error) via `@handle_modal_auth_error`. `xfail` these. |
| Bogus credentials | `ProviderUnavailableError` with curated `user_help_text` | **[INCONSISTENT]** Only Azure passes curated help text via `_azure_unavailable_error` (`libs/mngr_azure/imbue/mngr_azure/backend.py`). AWS still raises `ProviderUnavailableError(name, str(e))` with no `user_help_text` (`libs/mngr_aws/imbue/mngr_aws/backend.py`); GCP same (`libs/mngr_gcp/imbue/mngr_gcp/backend.py`), so both fall through to the default "start Docker" text (`ProviderUnavailableError`, `libs/mngr/imbue/mngr/errors.py:224`, default at `:247`). Assert the text mentions the provider-correct command (`aws configure` / `gcloud auth application-default login` / `az login` / `uvx modal token set`); Azure passes, `xfail` AWS+GCP+Modal. |
| Empty-but-reachable backend (e.g. Modal env exists, zero sandboxes) | `ProviderEmptyError`, listing silently skips | Modal is the only provider that hits this case naturally. |
| `mngr gc` with a provider whose creds are missing | Visible WARN-level message; non-zero exit OR visible error in summary | **[INCONSISTENT]** `mngr gc` currently DEBUG-logs `ProviderUnavailableError` (`libs/mngr/imbue/mngr/api/providers.py:211`-`212`); `mngr gc` itself does now exit non-zero on any failed sweep. `xfail` the WARN-visibility assertion. |
| Build arg with wrong provider prefix (e.g. `mngr create -p aws -b --vultr-region=ewr`) | `MngrError` with migration-style help text | All cloud providers correct via `raise_if_vps_migration_arg`. Symmetric strength. |
| `mngr stop --stop-host` on a provider where `supports_shutdown_hosts is False` | `HostShutdownNotSupportedError` | **[INCONSISTENT]** SSH provider returns `supports_shutdown_hosts=True` (`libs/mngr/imbue/mngr/providers/ssh/instance.py:105`) but `stop_host` raises `NotImplementedError` (`:184`-`:190`) — gate at `mngr/cli/stop.py:72` lets the call through and the user gets a stack trace. `xfail` SSH. |

---

## Coverage matrix vs `specs/provider-shape.md`

Mapping shape-doc sections to the trip step(s) that exercise them:

| Shape section | Trip(s) | Note |
|---|---|---|
| §1.1 `mngr create` | T1 step 2 | Exercised. |
| §1.2 `mngr list` (RUNNING/STOPPED/CRASHED/DESTROYED + credentials) | T1 steps 2/7/13, T4 | Now honest on AWS/Azure (state bucket) + GCP (metadata); stopped-host case still **broken on Vultr/OVH**. |
| §1.3 `mngr stop` (no flag) | T1 step 6 | Symmetric across all 9. |
| §1.4 `mngr stop --stop-host` (real stop OR loud refuse) | T1 step 7 | Now real on AWS/GCP/Azure; still **container-only on Vultr/OVH**. |
| §1.5 `mngr start` (idempotent, snapshot honor/refuse) | T1 step 8, T3 steps 5-6 | Snapshot-restore still silently no-ops on the VPS family (`create_host`/`start_host` ignore the arg). |
| §1.6 `mngr destroy` | T1 steps 14-15 | OVH is "cancel-at-expiration" not "destroy now". |
| §1.7 `mngr <provider> cleanup` | T1 step 1, T1 step 16 | Modal/Lima/Docker/SSH/Vultr have no equivalent. |
| §1.8 N agents on one host | T1b all | Modal does per-agent records; AWS/Azure via the state bucket (`BucketHostStateStore`) with the per-agent tag mirror as no-bucket fallback; GCP via metadata; Vultr/OVH inherit the plain base (no offline mirror). |
| §1.9 Error classes | T4 all | Vultr/OVH silent-empty, Modal wrong-class, AWS/GCP default help-text; Azure now curated. |
| §2.1 `supports_snapshots` | T1 step 11, T3 | Realizer-derived (container True, bare False); container-shape snapshot still `docker commit` that dies with the VPS. |
| §2.2 `supports_shutdown_hosts` | T1 step 7 | SSH lies; AWS/GCP/Azure now real stop; Vultr/OVH "True but only container". |
| §2.3 `supports_volumes` | T1 step 10 | True-but-empty on VPS family. |
| §3.1 Security defaults (`allowed_ssh_cidrs`) | Implicit in T1 step 1 setup | Asserted via `mngr <provider> prepare` refusing empty CIDR. AWS open default is the outlier (T1 step 1 should assert prepare warns). |
| §3.2 Idle timeout | T2 (implicit baseline) | Field present on 8/9 providers; symmetric. |
| §3.3 Auto-shutdown | T2 all | Now honest on AWS/GCP/Azure (Azure self-deallocates); still **OS-halt-bills on Vultr/OVH**. |
| §3.4 Resource defaults (disk size) | Implicit via create defaults | Three different field names; T1 step 2 explicitly does not override → exercises defaults. |
| §3.5 Region/zone defaults | Implicit | Each provider's settings.toml in the test fixture sets the region. |
| §3.6 Instance size defaults | Implicit | Same — not overridden, so default fires. |
| §3.7 Image defaults | Implicit | Same. |
| §3.8 Tagging conventions | T1 step 3 | Modal-vs-dash divergence. |
| §3.9 SSH key location | Implicit in T1 step 4 (must work) | Pinned by harness. |
| §3.10 Container exposure | Could add: scan VPS port 2222 from outside SSH cidr | **Not currently in any trip.** Suggested addition: T1 step 3a — outside-CIDR probe of container_ssh_port → expect refused. Today the VPS-family container path binds `0.0.0.0:22` in `DockerRealizer` so the probe would succeed on Vultr/OVH (no firewall) and fail correctly on AWS/Azure/GCP only when their firewalls are configured to deny outside the allowed CIDR. |
| §4 Lifecycle hooks (per-provider override correctness) | Implicit in T1 step 2 | Provider-specific corner cases (Azure NIC reclaim, OVH ordering) tested by provider-specific test files alongside the shared trips. |
| Isolation modes (`CONTAINER` vs `NONE`) | Trip 1 parametrized over isolation mode | Each cloud provider has two realizer shapes; bare-capable on AWS/GCP/Azure, rejected with `BareIsolationNotSupportedError` on Vultr/OVH; bare skips snapshots (`supports_snapshots=False`). |
| §5 Error classification | T4 | Same as §1.8. |
| §6 Operator commands | T1 step 1, T1 step 16 | Refusal semantics, idempotency. |
| §7 Test coverage requirements | This document | Self-referential — the shape doc says "every provider's test suite MUST pin orphan scanner, pytest gate, capability flags"; this trip set is the release-tier piece. The unit-test pinning belongs in `*_test.py`. |

**Total shape-doc points uniformly exercised after this proposal:** ~22 out of ~27. The 5 not exercised:

- §3.10 (container ingress) — proposal: add probe to T1 step 3a (open).
- The `--cpu`/`--memory`/`--gpu` Modal-only build args (§1.1 SHOULD) — release tests don't usefully exercise these; belongs in unit tests.
- The `auto_shutdown_seconds`-flows-through-to-cloud-API assertion — easier to pin at unit level.
- Per-host SSH key rotation across `start_host` (only AWS/Modal natively encounter; AWS's `_rebind_known_hosts` is the model) — already covered indirectly by T1 step 8 but no explicit assertion.
- `CleanupFailedGroup` raise-on-partial-failure — hard to provoke at the release tier without a chaos hook. Belongs in unit tests with mocked destroy helpers.

---

## Inconsistency callouts (explicit)

The trip definitions above flag every step where providers diverge today. Summary by severity:

### High — cost / security divergence the trip will surface

| Trip step | Inconsistent on | Symptom |
|---|---|---|
| T1.7 `--stop-host` cost-stop probe | Vultr, OVH | Inherited base only stops container; cloud-API state still `running` (AWS/GCP/Azure now real-stop — was an xfail, now passes) |
| T2.4 auto-shutdown cost-stop probe | Vultr, OVH | OS halts but VPS keeps billing (Azure now self-deallocates, GCP terminates — both pass; was an xfail for Azure) |
| T1.13 stopped-host visibility in `mngr list` | Vultr, OVH | Force-terminated instance vanishes from listing (AWS/Azure bucket + GCP metadata now keep it visible — was an xfail, now passes) |
| §3.10 container ingress probe (proposed) | Vultr, OVH (no firewall surface at all) | port 2222 reachable from public internet |
| T4 missing-creds raises `ProviderUnavailableError` | Vultr, OVH, Modal | Vultr/OVH silently return `[]`; Modal raises `ModalAuthError` |

### Medium — capability-flag honesty

| Trip step | Inconsistent on | Symptom |
|---|---|---|
| T1.7 `supports_shutdown_hosts` honesty | SSH (True but raises NotImplementedError) | User-visible stack trace |
| T1.10 `supports_volumes` non-empty list | AWS/Azure/GCP/Vultr/OVH (True but empty) | `list_volumes()` returns `[]` (AWS/Azure have `get_volume_for_host` via the bucket, but `list_volumes`/`delete_volume` are still unimplemented and the flag was not flipped) |
| T1.11/T3 `supports_snapshots` survives destroy | AWS/Azure/GCP/Vultr/OVH/Docker container shape (docker-commit, dies with VPS); bare shape correctly reports `supports_snapshots=False` | `mngr snapshot list` after destroy returns nothing |
| T3.5 `--snapshot` at `mngr create` | AWS/Azure/GCP/Vultr/OVH | Silently no-ops (`create_host`/`start_host` accept the arg but never use it) |
| T1.x bare-isolation gate | Vultr, OVH (`_supports_bare_isolation=False`) | `mngr create --... isolation=NONE` must raise `BareIsolationNotSupportedError` (this is the correct, asserted behavior — not a divergence to fix) |
| T4 `mngr stop --stop-host` on SSH | SSH | NotImplementedError stack trace (not `HostShutdownNotSupportedError`) |
| T1.16 prepare/cleanup idempotency | Cloud trio honest. Modal/Lima/Docker/SSH/Vultr skip — no command. | (n/a — skip cleanly) |
| T1b.3 per-agent persistence cap | tag-mirror fallback (no bucket): EC2 50-tag wall + 256-char `labels` drop; SSH (no persisted store) | full records on the bucket path; `labels` dropped on the tag-fallback path; empty for SSH |
| T1b.5 offline mirror shows N agents while stopped | Vultr/OVH (no offline mirror); SSH (no offline view) | `mngr list` shows zero agents on stopped host (AWS/Azure bucket + GCP metadata now pass — was an xfail for Azure/GCP) |

### Low — UX inconsistencies the trip will document

| Trip step | Inconsistent on | Symptom |
|---|---|---|
| T1.3 `mngr-host-id` tag probe | Modal uses `mngr_host_id` (underscore) | Probe must accept both |
| T4 `ProviderUnavailableError` help text | AWS, GCP, Modal | Default "start Docker" message; only Azure curated |
| T1.15 backend-resource cleanup | OVH | "Cancel at expiration", not "destroy now" |
| T4 `mngr gc` with bad creds | All providers | DEBUG-only log; `gc` reports 0 resources silently |

---

## Implementation notes

- **Shared trip body lives in `libs/mngr_vps/imbue/mngr_vps/testing.py`** (or a new `release_trips.py` if the existing testing helper is too crowded). The trip body is a generator-style sequence of named steps so the harness can `pytest.skip` per step rather than per test. Parametrize over `IsolationMode` so each trip runs against both the `DockerRealizer` and `BareRealizer` shapes (see *Trip 1, parametrized over isolation mode*).
- **Per-provider release test file** (`test_release_<provider>.py`) shrinks to a thin parametrize harness — pass the provider fixture in, mark with `pytest.mark.release`, mark which trip steps the provider `xfail`s with a one-line reason citing this doc.
- **Skip vs `xfail`.** Use `skip` when the provider documentably doesn't claim the capability (Modal/Lima/Docker/SSH skip `prepare`; SSH skips `create`). Use `xfail` when the provider claims to support something but the implementation diverges — that way the test runs, the divergence is detected, and once fixed the test starts passing without any churn in this proposal. This is also how the stop/start, stopped-host-visibility, and idle-deallocate findings get pinned: each goes from `xfail` to `pass` when the fix lands. Several have already flipped with the `mngr/bare-providers` merge — the AWS/GCP/Azure `--stop-host` real-stop (T1.7), stopped-host visibility (T1.13), Azure idle deallocate (T2.4), and AWS/Azure/GCP offline agent mirror (T1b.5) now pass — so the harness should encode those as hard assertions, not `xfail`s; only Vultr/OVH (and SSH/Modal where noted) remain `xfail`.
- **Sketchy-kill mechanism** in T1 step 12 needs a per-provider hook (`provider_test_fixture.force_strand_resource(host_id)`). Implementations:
  - AWS: `ec2:TerminateInstances` directly.
  - Azure: `virtual_machines.begin_delete` directly.
  - GCP: `instances.delete` directly.
  - Vultr: `delete_instance` directly.
  - OVH: cancel order out of band.
  - Modal: Modal SDK direct sandbox kill.
  - Lima: `limactl delete --force`.
  - Docker: `docker rm -f`.
  The sketchy-kill destroy path now lives behind the realizer seam (`DockerRealizer.teardown_placement` in `libs/mngr_vps/imbue/mngr_vps/docker_realizer.py`; provider `destroy_host` at `instance.py:1325` calls the VPS client `destroy_instance`/`delete_ssh_key`), so the out-of-band kill must go around `mngr destroy` and hit the cloud API or realizer directly.
- **Cost-stop probes** in T1.7 and T2.4 need a per-provider hook (`provider_test_fixture.assert_compute_billing_stopped(host_id)`). For Vultr/OVH this probe will still fail — that's the point. AWS/GCP/Azure now stop/deallocate the VM, so the probe passes there (and is already exercised by the AWS bare `test_bare_provider_stop_host_stops_ec2_instance...` / `..._idle_watcher_auto_stops...` tests).
- **Estimated wall-clock per provider with the new trip set:**
  - Trip 1: ~15 min (one boot + steps + sketchy-kill + GC)
  - Trip 2: ~5 min (one boot, short auto-shutdown wait)
  - Trip 3: ~10 min (one boot, snapshot, second boot from snapshot)
  - Trip 4: ~30 sec (no boot, pure CLI)
  - Total: ~30 min wall clock per provider (vs ~45-90 min today across the 4-6 separate lifecycle tests). Concurrent across providers, this is single-digit-minute CI cost increase per provider added.
- **Existing release tests** collapse into Trip 1 + Trip 2 (parametrized over isolation mode). The container-shape lifecycle tests (`test_provider_lifecycle_create_exec_and_destroy`, `test_provider_lifecycle_create_stop_start_destroy`, `test_provider_stop_host_stops_ec2_instance_and_start_resumes`, `test_provider_idle_watcher_auto_stops_then_resumes`) become the `isolation=CONTAINER` parametrization, and the already-landed bare tests (`test_bare_provider_lifecycle_create_exec_and_destroy` on AWS/GCP/Azure; AWS additionally `test_bare_provider_stop_host_stops_ec2_instance_and_start_resumes` and `test_bare_provider_idle_watcher_auto_stops_then_resumes`) become the `isolation=NONE` parametrization. Don't delete them in the same PR; delete in a follow-up once the trips are stable across all providers.
- **Vultr/OVH `pytest_sessionfinish` orphan scanner** is still a prerequisite (still missing on both — AWS/Azure/GCP all define one). The shared harness should refuse to run release tests against a provider whose conftest doesn't register one — `pytest_sessionstart` check.

---

## Open questions

1. **`xfail`-driven discovery vs. blocking the CI gate.** With the inconsistencies surfaced above, Trip 1 will `xfail` 6+ steps across 4 providers from day 1. Is that the right signal (CI green, divergence visible in `xfail` reasons)? Or should the trip be hard-gated on shape-doc compliance and providers run the trip in "stash" mode until fixed? Recommendation: `xfail` initially, hard-fail after a documented deadline per finding.
2. **Should the cost-stop probe assert a `mngr_cost.assert_provider_stopped_billing(host_id)` interface** that each provider implements, or should each provider's `provider_test_fixture` expose the probe directly? The former unifies the assertion shape; the latter is simpler to add.
3. **Trip 1 step 3a (container ingress probe)** requires reaching the VPS from an IP not in the test's `allowed_ssh_cidrs`. Easiest implementation: probe from a fixed cloud-test IP that the CI definitionally has, with that IP excluded from the test's CIDR. Worth doing, or out of scope for the release tier?
4. **For Modal, the trip's `--stop-host` step** asserts `HostShutdownNotSupportedError`. Should the trip ALSO assert that `mngr destroy + mngr create --snapshot <id>` is the documented Modal equivalent? That's a behavioral check ("Modal users have a workaround that gives parity"); could be added as Trip 1 step 7b (Modal-only).
5. **Per-provider build args** — Trip 1 boots with provider-specific build args (e.g. `--aws-instance-type=t3.small`). Should the trip set include a variant that boots with **default** instance size to exercise §3.6, or should that always be the case? Recommendation: default-size is the baseline; an optional `pytest.mark.parametrize` over a few representative sizes can fan out in a separate `test_release_<provider>_sizes.py`.
6. **Modal's auto-snapshot-on-create** (`is_snapshotted_after_create=True`) means Trip 3 starts with a snapshot already present. Worth asserting in step 1.11 that Modal returns the auto-snapshot in `mngr snapshot list`, while the cloud trio returns empty? That bakes the "Modal is alone in auto-snapshotting" finding into the test.
