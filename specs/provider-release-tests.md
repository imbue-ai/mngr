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

Provide a shared `libs/mngr_vps_docker/imbue/mngr_vps_docker/testing.py::ProviderReleaseTrips` mixin (or equivalent flat conftest fixture in `libs/mngr_vps_docker/conftest.py`) that:

1. **Parametrizes by provider.** Each provider's `test_release_<name>.py` calls into the shared trips with a provider-specific fixture supplying `(provider_name, mngr_ctx, settings_toml_extras, expected_capabilities)`.
2. **Reports skip reason with shape-doc cite.** When a trip step is gated by a capability flag (`supports_shutdown_hosts`, `supports_snapshots`, `supports_volumes`) or a documented provider quirk, the harness calls `pytest.skip(f"shape §X.Y: {provider_name} does not support {capability}")` so the skip reason is greppable.
3. **Records cost-impact assertions.** When a step asserts "compute billing stops", the harness calls the provider's cost-stop probe (e.g. `aws_client.describe_instance_state(id) == "stopped"`) — see *Inconsistency callouts* below for which providers can satisfy this honestly today.
4. **Tags every test-launched cloud resource** with `mngr-pytest-launched=true` and runs in a `MNGR_PROJECT_CONFIG_DIR=<tmpdir>` settings.toml that sets `auto_shutdown_seconds=3600`. Pairs with each provider's existing `pytest_sessionfinish` orphan scanner; **gates Vultr/OVH on adding one** (currently missing).

The trip body itself is provider-agnostic — it speaks only through `mngr` CLI + `provider.<methods>` + the cloud-API probe.

---

## The four trips

Each trip is a numbered sequence. Provider-specific inconsistencies are flagged inline with **[INCONSISTENT]** markers and a follow-on note pointing at the shape-doc section and the cite for current behavior.

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
     - **[INCONSISTENT]** AWS overrides `stop_host` and actually stops the EC2 instance; cost probe expected to succeed. Azure/GCP/Vultr/OVH inherit the base which only stops the container — cost probe **WILL FAIL** today. `xfail` with reason `"shape §1.4: <provider> claims supports_shutdown_hosts but only stops container"` until the override lands, then flip to a hard fail.
8. **Start.** `mngr start <name>` → assert exit 0. Verify `mngr list` shows host as `RUNNING`. Run `mngr start <name>` a second time → assert exit 0 and no error (idempotent, shape §1.5).
9. **Persistence check after stop+start.** `mngr exec <name> 'cat /tmp/trip1-marker.txt'` → expect `trip1-marker`. `mngr transcript <name>` → expect the prompt from step 5 (shape §1.5).
10. **Capability-flag honesty: volumes.** `provider.list_volumes()`:
    - If `supports_volumes is False`: expect raise or specific error.
    - If `supports_volumes is True`: expect a non-empty list including this host's mount.
      - **[INCONSISTENT]** AWS/Azure/GCP/Vultr/OVH inherit `supports_volumes=True` but `list_volumes()` returns `[]` (`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py:2215-2219`). `xfail` until either the flag is flipped to `False` or the implementation lands.
11. **Capability-flag honesty: snapshots.** Skip if `not provider.supports_snapshots`. Otherwise: `mngr snapshot create <name>` → assert returns a `SnapshotId`. `mngr snapshot list <name>` → assert it appears.
    - **[INCONSISTENT]** For AWS/Azure/GCP/Vultr/OVH/Docker the snapshot is a `docker commit` and will not survive `destroy_host` — see Trip 3 for the survive-destroy check. For Modal, the snapshot is portable and DOES survive destroy.
12. **Sketchy kill.** Out-of-band corrupt the host. Pick the sketchiest mechanism the provider exposes:
    - Cloud providers: call the cloud-API directly to force-terminate the instance/VM (bypasses `mngr destroy`, leaves the on-VPS state volume in place but unreachable).
    - Modal: kill the sandbox via Modal SDK without going through `mngr destroy`.
    - Lima: `limactl delete --force` directly.
    - Docker: `docker rm -f <container>` directly.
13. **Discovery reflects the kill.** `mngr list` → host appears as `CRASHED` (shape §1.2). For AWS specifically, the tag-based offline-host reconstruction should fall back gracefully.
    - **[INCONSISTENT]** Azure/GCP/Vultr/OVH inherit base discovery, which **drops anything without a current public IP**, so a force-terminated instance vanishes from `mngr list` entirely. The shape doc says hosts MUST stay visible across all states (§1.2). `xfail` for the four affected providers; AWS is honest.
14. **`mngr gc` reclaims the orphan.** `mngr gc` → assert exit 0. Assert `mngr list --include-destroyed` shows the host as `DESTROYED` (or absent for the providers that don't persist destroyed records).
15. **Verify backend is clean.** Probe cloud-API: no instance with `mngr-host-id=<id>`, no leaked NIC/IP/EBS volume (shape §1.6). For AWS specifically: per-host KeyPair removed.
    - **[INCONSISTENT]** OVH `destroy_host` is "cancel at expiration", not "destroy now". OVH's verify step needs a separate "VPS will expire on date X" assertion, not "VPS is gone". Trip should `xfail` the immediate-cleanup assertion for OVH and assert the cancellation flag instead.
16. **Cleanup refuses if any resources remain.** Skip if no `cleanup` analog. Otherwise: ensure all test hosts destroyed, then `mngr <provider> cleanup` → exit 0, region clean.
    - Bonus: before step 14, run `mngr <provider> cleanup` and assert it **refuses** with a `mngr destroy <agent>` pointer (shape §1.7 MUST).

### Trip 1b — "Second agent on the same host" (piggy-backs on Trip 1)

Inserted between Trip 1 step 9 (persistence check) and Trip 1 step 11 (capability flag checks). **No new boot** — uses the host Trip 1 already provisioned. ~2-3 min wall clock.

Exercises shape doc §1.8 (N agents per host).

1b.1 **Add a second agent.** `mngr exec <host> --new-agent 'echo "trip1b-agent-2" > /tmp/trip1b-agent-2.txt'`. Assert exit 0.
1b.2 **Both agents visible live.** `mngr list` shows two agents under `<host>` with distinct `agent_id`s. Live discovery via in-container scan (`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py:1506-1565`) works uniformly across the VPS family.
1b.3 **Per-agent persisted records.** Probe `provider.list_persisted_agent_data_for_host(host_id)` → assert length 2 with distinct `id`s.
   - **Per-provider citation.** Modal: `/hosts/{host_id}/{agent_id}.json` on state volume (`libs/mngr_modal/imbue/mngr_modal/instance.py:785-804`). AWS: per-field tags `mngr-agent-<id>-name`/`-type`/`-labels` (`libs/mngr_aws/imbue/mngr_aws/backend.py:660-687`). Azure/GCP/Vultr/OVH/Docker/Lima: base `DockerHostStore.persist_agent_data` (`libs/mngr/imbue/mngr/providers/docker/host_store.py:160-170`).
   - **[INCONSISTENT — capped]** AWS hits its EC2 50-tag cap at ~16 agents, then raises `NotImplementedError` (`libs/mngr_aws/imbue/mngr_aws/backend.py:677-685`). Trip 1b should fork a parametrized variant `trip1b_at_capacity` that loops to N=20 and asserts the right error fires. SSH provider has no persisted store at all — Trip 1b runs against SSH but step 1b.3 is `xfail`'d with reason "shape §1.8: SSH has no offline mirror".
1b.4 **Stop-cycle preserves both.** `mngr stop <host> --stop-host` (where supported) → `mngr start <host>` → `mngr exec <host> --agent <agent-2-id> 'cat /tmp/trip1b-agent-2.txt'` returns `trip1b-agent-2`. Also assert agent-1's file from Trip 1 step 4 still present.
1b.5 **Offline mirror shows N agents while VPS is stopped.** After step 1b.4's stop (before the start), `mngr list <host>` → assert two agents still visible.
   - **[INCONSISTENT — high]** Only Modal and AWS pass this step today. Azure/GCP/Vultr/OVH inherit `VpsDockerProvider.list_persisted_agent_data_for_host` which raises `HostNotFoundError` when the VPS IP is unreachable (`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py:2402-2405`). `xfail` for those four. The data is intact on the volume; it's just invisible. Once VM-level stop lands on those providers, Trip 1b.5 will start failing visibly — desired forcing function.
   - SSH `xfail`: no offline view at all (`libs/mngr/imbue/mngr/providers/ssh/instance.py:208-216` FIXME).
1b.6 **Destroying one agent leaves the other.** `mngr destroy --agent <agent-2-id>` (or whatever the per-agent destroy verb is). Assert agent-1 still listed, its file still readable. Assert `provider.list_persisted_agent_data_for_host` now returns length 1.
   - If no per-agent destroy verb exists in the CLI today, document the gap as a finding and proceed.
1b.7 **Trip 1 continues** at step 11. Trip 1 step 14 (sketchy kill) and 15 (cleanup) will verify both agents go away together when the host is destroyed (shape §1.8 "destroy_host MUST iterate all agents").

### Trip 2 — "Idle auto-shutdown contract"

**Goal:** assert `auto_shutdown_seconds` honestly stops billing. **One boot, ~5 minute wall clock** (use shortest acceptable interval).

Exercises shape doc §3.3 (auto-shutdown defaults), §1.4 (cost stop), §2.2 (`supports_shutdown_hosts` honesty under idle).

1. **Create with short auto-shutdown.** `mngr create <name> --provider <provider>`, with settings.toml setting `auto_shutdown_seconds=120` (2 min).
2. **Verify host running.** `mngr list` shows `RUNNING`.
3. **Wait > auto-shutdown.** `sleep 180`.
4. **Verify host stopped/terminated/deleted.** Probe cloud-API for the provider-specific cost-stop state:
   - AWS: instance state `stopped` (with `terminate_on_shutdown=false`) or `terminated` (with `terminate_on_shutdown=true`). Verify EBS still present in former case.
   - GCP: instance state `TERMINATED` (deleted via `instance_termination_action=DELETE`).
   - Azure: **[INCONSISTENT]** VM state `Stopped (not deallocated)` (still billing!). The shape doc says `auto_shutdown_seconds` MUST actually stop billing (§3.3). `xfail` until Azure managed-identity self-delete lands.
   - Vultr: **[INCONSISTENT]** OS halts but VPS keeps billing hourly. `xfail`.
   - OVH: **[INCONSISTENT]** Same — OS halt, VPS keeps billing for the month. `xfail`.
   - Modal: sandbox terminated by Modal's own timeout. Probe: Modal client reports sandbox gone.
   - Lima: no `auto_shutdown_seconds` field — skip with `pytest.skip("shape §3.3: lima has no auto-shutdown")`.
   - Docker: no field — skip.
   - SSH: no field — skip.
5. **`mngr start` after auto-shutdown.** If provider supports resume from stopped (AWS-only currently honest): `mngr start <name>` → assert exit 0. Verify host present and file from a pre-shutdown step is intact.
6. **Destroy.** `mngr destroy <name>` → clean.

### Trip 3 — "Snapshot survives destroy" (snapshot-supporting providers only)

**Goal:** assert that a "snapshot" is actually a snapshot — survives `destroy_host` and can be used by a fresh `mngr create --snapshot <id>`. **One boot + one re-boot, ~10 minute wall clock.**

Exercises shape doc §1.5 (`start_host(snapshot_id=…)`), §1.6 (snapshot MAY survive destroy), §2.1 (`supports_snapshots` honesty).

1. **Skip if `not provider.supports_snapshots`.** SSH/Lima skip; everyone else runs.
2. **Create + write file + snapshot.** `mngr create`, `mngr exec <name> 'echo trip3 > /tmp/trip3.txt'`, `mngr snapshot create <name>` → captures `snapshot_id`.
3. **Destroy.** `mngr destroy <name>`.
4. **Verify snapshot record persists.** `mngr snapshot list` (without `<host>`) → assert snapshot still present.
   - **[INCONSISTENT]** Modal preserves snapshot records intentionally (`libs/mngr_modal/imbue/mngr_modal/instance.py:2074-2078`). AWS/Azure/GCP/Vultr/OVH/Docker `docker commit` snapshots live on the VPS's own disk and die with the VPS — the record vanishes. Trip should `xfail` step 4 for those providers and add an explicit assert "snapshot record absent after destroy" so the test documents what the user gets.
5. **Restore.** `mngr create <new-name> --snapshot <snapshot_id>` → assert exit 0.
   - **[INCONSISTENT]** Only Modal and Docker honor `--snapshot` at create time. AWS/Azure/GCP/Vultr/OVH base path silently ignores the parameter. Shape §1.5 says either honor or raise — silent no-op is the worst option. `xfail`.
6. **Verify file restored.** `mngr exec <new-name> 'cat /tmp/trip3.txt'` → expect `trip3`.
7. **Cleanup.** `mngr destroy <new-name>`, `mngr snapshot destroy --snapshot <id>`.

### Trip 4 — "Error classification contract"

**Goal:** assert that `mngr list` / `mngr gc` / `mngr create` raise the right error class for each failure mode. **No boot — pure CLI exercise.**

Exercises shape doc §1.2 (ProviderEmpty vs Unavailable), §1.8 (error class for each failure), §3 (Setup) — what the user sees when their credentials are wrong.

For each scenario, run `mngr list` and assert the expected error class **or** the expected silent-skip behavior:

| Scenario | Expected | Inconsistencies |
|---|---|---|
| No `[providers.<X>]` block at all | `ProviderEmptyError` if env-derivable; `ProviderUnavailableError` if creds missing | **[INCONSISTENT]** Vultr / OVH silently return `[]` instead of raising. Modal raises `ModalAuthError` (a `PluginMngrError`, not the contract error). `xfail` these. |
| Bogus credentials | `ProviderUnavailableError` with curated `user_help_text` | **[INCONSISTENT]** Only Azure passes curated help text via `_azure_unavailable_error`. AWS/GCP fall through to the default "start Docker" text. Assert the text mentions the provider-correct command (`aws configure` / `gcloud auth application-default login` / `az login` / `uvx modal token set`); `xfail` AWS+GCP+Modal. |
| Empty-but-reachable backend (e.g. Modal env exists, zero sandboxes) | `ProviderEmptyError`, listing silently skips | Modal is the only provider that hits this case naturally. |
| `mngr gc` with a provider whose creds are missing | Visible WARN-level message; non-zero exit OR visible error in summary | **[INCONSISTENT]** `mngr gc` currently DEBUG-logs `ProviderUnavailableError` (`libs/mngr/imbue/mngr/api/providers.py:211-213`); `mngr gc` itself does now exit non-zero on any failed sweep. `xfail` the WARN-visibility assertion. |
| Build arg with wrong provider prefix (e.g. `mngr create -p aws -b --vultr-region=ewr`) | `MngrError` with migration-style help text | All cloud providers correct via `raise_if_vps_migration_arg`. Symmetric strength. |
| `mngr stop --stop-host` on a provider where `supports_shutdown_hosts is False` | `HostShutdownNotSupportedError` | **[INCONSISTENT]** SSH provider returns `supports_shutdown_hosts=True` but `stop_host` raises `NotImplementedError` — gate at `mngr/cli/stop.py:72` lets the call through and the user gets a stack trace. `xfail` SSH. |

---

## Coverage matrix vs `specs/provider-shape.md`

Mapping shape-doc sections to the trip step(s) that exercise them:

| Shape section | Trip(s) | Note |
|---|---|---|
| §1.1 `mngr create` | T1 step 2 | Exercised. |
| §1.2 `mngr list` (RUNNING/STOPPED/CRASHED/DESTROYED + credentials) | T1 steps 2/7/13, T4 | Stopped-host case **broken on 4 providers** today. |
| §1.3 `mngr stop` (no flag) | T1 step 6 | Symmetric across all 9. |
| §1.4 `mngr stop --stop-host` (real stop OR loud refuse) | T1 step 7 | **Broken on Azure/GCP/Vultr/OVH** today. |
| §1.5 `mngr start` (idempotent, snapshot honor/refuse) | T1 step 8, T3 steps 5-6 | Snapshot-restore silently no-ops on 6 providers. |
| §1.6 `mngr destroy` | T1 steps 14-15 | OVH is "cancel-at-expiration" not "destroy now". |
| §1.7 `mngr <provider> cleanup` | T1 step 1, T1 step 16 | Modal/Lima/Docker/SSH/Vultr have no equivalent. |
| §1.8 N agents on one host | T1b all | Modal does per-agent records; AWS via per-agent tags (commit `446d6a964`); other providers inherit base — actual N-agent correctness TBD per provider. |
| §1.9 Error classes | T4 all | Vultr/OVH silent-empty, Modal wrong-class, AWS/GCP default help-text. |
| §2.1 `supports_snapshots` | T1 step 11, T3 | Shape claims True implies useful snapshot; today not true for VPS family. |
| §2.2 `supports_shutdown_hosts` | T1 step 7 | SSH lies; Azure/GCP/Vultr/OVH "True but only container". |
| §2.3 `supports_volumes` | T1 step 10 | True-but-empty on VPS family. |
| §3.1 Security defaults (`allowed_ssh_cidrs`) | Implicit in T1 step 1 setup | Asserted via `mngr <provider> prepare` refusing empty CIDR. AWS open default is the outlier (T1 step 1 should assert prepare warns). |
| §3.2 Idle timeout | T2 (implicit baseline) | Field present on 8/9 providers; symmetric. |
| §3.3 Auto-shutdown | T2 all | **Broken on Azure/Vultr/OVH** today. |
| §3.4 Resource defaults (disk size) | Implicit via create defaults | Three different field names; T1 step 2 explicitly does not override → exercises defaults. |
| §3.5 Region/zone defaults | Implicit | Each provider's settings.toml in the test fixture sets the region. |
| §3.6 Instance size defaults | Implicit | Same — not overridden, so default fires. |
| §3.7 Image defaults | Implicit | Same. |
| §3.8 Tagging conventions | T1 step 3 | Modal-vs-dash divergence. |
| §3.9 SSH key location | Implicit in T1 step 4 (must work) | Pinned by harness. |
| §3.10 Container exposure | Could add: scan VPS port 2222 from outside SSH cidr | **Not currently in any trip.** Suggested addition: T1 step 3a — outside-CIDR probe of container_ssh_port → expect refused. Today VPS-Docker family binds `0.0.0.0:2222` so the probe would succeed on Vultr/OVH (no firewall) and fail correctly on AWS/Azure/GCP only when their firewalls are configured to deny outside the allowed CIDR. |
| §4 Lifecycle hooks (per-provider override correctness) | Implicit in T1 step 2 | Provider-specific corner cases (Azure NIC reclaim, OVH ordering) tested by provider-specific test files alongside the shared trips. |
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
| T1.7 `--stop-host` cost-stop probe | Azure, GCP, Vultr, OVH | Inherited base only stops container; cloud-API state still `running` |
| T2.4 auto-shutdown cost-stop probe | Azure, Vultr, OVH | OS halts but VM/VPS keeps billing |
| T1.13 stopped-host visibility in `mngr list` | Azure, GCP, Vultr, OVH | Force-terminated instance vanishes from listing |
| §3.10 container ingress probe (proposed) | Vultr, OVH (no firewall surface at all) | port 2222 reachable from public internet |
| T4 missing-creds raises `ProviderUnavailableError` | Vultr, OVH, Modal | Vultr/OVH silently return `[]`; Modal raises `ModalAuthError` |

### Medium — capability-flag honesty

| Trip step | Inconsistent on | Symptom |
|---|---|---|
| T1.7 `supports_shutdown_hosts` honesty | SSH (True but raises NotImplementedError) | User-visible stack trace |
| T1.10 `supports_volumes` non-empty list | AWS/Azure/GCP/Vultr/OVH (True but empty) | `list_volumes()` returns `[]` |
| T1.11/T3 `supports_snapshots` survives destroy | AWS/Azure/GCP/Vultr/OVH/Docker (docker-commit, dies with VPS) | `mngr snapshot list` after destroy returns nothing |
| T3.5 `--snapshot` at `mngr create` | AWS/Azure/GCP/Vultr/OVH | Silently no-ops |
| T4 `mngr stop --stop-host` on SSH | SSH | NotImplementedError stack trace (not `HostShutdownNotSupportedError`) |
| T1.16 prepare/cleanup idempotency | Cloud trio honest. Modal/Lima/Docker/SSH/Vultr skip — no command. | (n/a — skip cleanly) |
| T1b.3 per-agent persistence cap | AWS (~16 agents max, EC2 50-tag wall); SSH (no persisted store) | NotImplementedError at cap; empty for SSH |
| T1b.5 offline mirror shows N agents while stopped | Azure/GCP/Vultr/OVH (no offline mirror); SSH (no offline view) | `mngr list` shows zero agents on stopped host |

### Low — UX inconsistencies the trip will document

| Trip step | Inconsistent on | Symptom |
|---|---|---|
| T1.3 `mngr-host-id` tag probe | Modal uses `mngr_host_id` (underscore) | Probe must accept both |
| T4 `ProviderUnavailableError` help text | AWS, GCP, Modal | Default "start Docker" message; only Azure curated |
| T1.15 backend-resource cleanup | OVH | "Cancel at expiration", not "destroy now" |
| T4 `mngr gc` with bad creds | All providers | DEBUG-only log; `gc` reports 0 resources silently |

---

## Implementation notes

- **Shared trip body lives in `libs/mngr_vps_docker/imbue/mngr_vps_docker/testing.py`** (or a new `release_trips.py` if the existing testing helper is too crowded). The trip body is a generator-style sequence of named steps so the harness can `pytest.skip` per step rather than per test.
- **Per-provider release test file** (`test_release_<provider>.py`) shrinks to a thin parametrize harness — pass the provider fixture in, mark with `pytest.mark.release`, mark which trip steps the provider `xfail`s with a one-line reason citing this doc.
- **Skip vs `xfail`.** Use `skip` when the provider documentably doesn't claim the capability (Modal/Lima/Docker/SSH skip `prepare`; SSH skips `create`). Use `xfail` when the provider claims to support something but the implementation diverges — that way the test runs, the divergence is detected, and once fixed the test starts passing without any churn in this proposal. This is also how F-STOP-1 / F-D-5 / N-3 / N-4 get pinned: each goes from `xfail` to `pass` when the fix lands.
- **Sketchy-kill mechanism** in T1 step 12 needs a per-provider hook (`provider_test_fixture.force_strand_resource(host_id)`). Implementations:
  - AWS: `ec2:TerminateInstances` directly.
  - Azure: `virtual_machines.begin_delete` directly.
  - GCP: `instances.delete` directly.
  - Vultr: `delete_instance` directly.
  - OVH: cancel order out of band.
  - Modal: Modal SDK direct sandbox kill.
  - Lima: `limactl delete --force`.
  - Docker: `docker rm -f`.
- **Cost-stop probes** in T1.7 and T2.4 need a per-provider hook (`provider_test_fixture.assert_compute_billing_stopped(host_id)`). For Azure/Vultr/OVH this probe will fail — that's the point.
- **Estimated wall-clock per provider with the new trip set:**
  - Trip 1: ~15 min (one boot + steps + sketchy-kill + GC)
  - Trip 2: ~5 min (one boot, short auto-shutdown wait)
  - Trip 3: ~10 min (one boot, snapshot, second boot from snapshot)
  - Trip 4: ~30 sec (no boot, pure CLI)
  - Total: ~30 min wall clock per provider (vs ~45-90 min today across the 4-6 separate lifecycle tests). Concurrent across providers, this is single-digit-minute CI cost increase per provider added.
- **Existing release tests** (`test_provider_lifecycle_create_exec_and_destroy`, `test_provider_lifecycle_create_stop_start_destroy`, `test_provider_stop_host_stops_ec2_instance_and_start_resumes`, `test_provider_idle_watcher_auto_stops_then_resumes`) collapse into Trip 1 + Trip 2. Don't delete them in the same PR; delete in a follow-up once the trips are stable across all providers.
- **Vultr/OVH `pytest_sessionfinish` orphan scanner** is a prerequisite (currently missing on both). The shared harness should refuse to run release tests against a provider whose conftest doesn't register one — `pytest_sessionstart` check.

---

## Open questions

1. **`xfail`-driven discovery vs. blocking the CI gate.** With the inconsistencies surfaced above, Trip 1 will `xfail` 6+ steps across 4 providers from day 1. Is that the right signal (CI green, divergence visible in `xfail` reasons)? Or should the trip be hard-gated on shape-doc compliance and providers run the trip in "stash" mode until fixed? Recommendation: `xfail` initially, hard-fail after a documented deadline per finding.
2. **Should the cost-stop probe assert a `mngr_cost.assert_provider_stopped_billing(host_id)` interface** that each provider implements, or should each provider's `provider_test_fixture` expose the probe directly? The former unifies the assertion shape; the latter is simpler to add.
3. **Trip 1 step 3a (container ingress probe)** requires reaching the VPS from an IP not in the test's `allowed_ssh_cidrs`. Easiest implementation: probe from a fixed cloud-test IP that the CI definitionally has, with that IP excluded from the test's CIDR. Worth doing, or out of scope for the release tier?
4. **For Modal, the trip's `--stop-host` step** asserts `HostShutdownNotSupportedError`. Should the trip ALSO assert that `mngr destroy + mngr create --snapshot <id>` is the documented Modal equivalent? That's a behavioral check ("Modal users have a workaround that gives parity"); could be added as Trip 1 step 7b (Modal-only).
5. **Per-provider build args** — Trip 1 boots with provider-specific build args (e.g. `--aws-instance-type=t3.small`). Should the trip set include a variant that boots with **default** instance size to exercise §3.6, or should that always be the case? Recommendation: default-size is the baseline; an optional `pytest.mark.parametrize` over a few representative sizes can fan out in a separate `test_release_<provider>_sizes.py`.
6. **Modal's auto-snapshot-on-create** (`is_snapshotted_after_create=True`) means Trip 3 starts with a snapshot already present. Worth asserting in step 1.11 that Modal returns the auto-snapshot in `mngr snapshot list`, while the cloud trio returns empty? That bakes the "Modal is alone in auto-snapshotting" finding into the test.
