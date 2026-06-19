# Common Provider Release Test Suite — Remaining Gaps

**Status.** The bulk of this proposal has LANDED. The shared trip harness
`run_provider_release_trip1` / `run_provider_release_trip2` / `run_provider_release_trip3` /
`run_provider_release_trip4` (`libs/mngr/imbue/mngr/providers/provider_release_testing.py`,
keyed off a per-provider `ProviderReleaseProfile`) now owns the trip bodies and assertions, and the
cloud trio (AWS/GCP/Azure) runs Trips 1-3 parametrized over `IsolationMode.CONTAINER` and
`IsolationMode.NONE` plus Trip 4, via `test_provider_release_trip1..4` in each provider's
`test_release_<name>.py` and the shared `VpsCloudReleaseProfile`
(`libs/mngr_vps/imbue/mngr_vps/testing.py`). Modal runs all four trips unparametrized
(`_ModalReleaseProfile` in `libs/mngr_modal/imbue/mngr_modal/test_release_modal.py`).

What landed (no longer tracked here): the four-trip consolidation; the container-vs-bare
parametrization for the cloud trio (the old per-provider `test_bare_provider_*` and
`test_provider_lifecycle_*` tests folded into the harness); the cost-stop probe
(`is_host_compute_stopped`) for `--stop-host` and idle auto-shutdown on AWS/GCP/Azure; stopped-host
visibility / offline reconstruction on the cloud trio; the curated `ProviderUnavailableError` help
text for AWS/GCP/Azure (Trip 4); the migration-arg refusal check; and the force-strand sketchy-kill
+ gc arc. The per-`[INCONSISTENT]` / xfail bookkeeping in the old draft is moot: divergences are now
encoded as capability booleans on the profile (e.g. `supports_shutdown_hosts`,
`snapshot_survives_destroy`, `has_curated_unavailable_help`), and where a provider documentably
diverges the harness asserts the divergence directly so it flips loudly when fixed.

This doc now tracks only what is **not yet implemented**.

---

## Remaining gaps

### 1. Vultr and OVH are not on the shared harness

Vultr (`libs/mngr_vultr/imbue/mngr_vultr/test_release_vultr.py`) and OVH
(`libs/mngr_ovh/imbue/mngr_ovh/test_release_ovh.py`) still run their own bespoke
create/exec/stop/start/destroy tests and define no `ProviderReleaseProfile`, so they get none of the
trip coverage. Putting them on the harness requires first closing the underlying provider gaps that
the trips would assert against (these are real behavioral gaps, not just missing tests — see
`specs/provider-shape.md`):

- **No `pytest_sessionfinish` orphan scanner.** AWS/Azure/GCP/Modal each register one in their
  conftest; Vultr/OVH do not, so a leaked VPS from a failed run is never swept. A scanner is the
  prerequisite for safely running money-spending trips against these providers.
- **`stop_host` / `start_host` only stop the container, not the VPS.** Vultr/OVH inherit the base
  and never power the VM off, so `--stop-host` and idle auto-shutdown leave the VPS billing. Trip 1
  step 6 and Trip 2's `is_host_compute_stopped` probe would fail. They still report
  `supports_shutdown_hosts = True` (honesty gap).
- **Stopped/force-terminated hosts vanish from `mngr list`.** Vultr/OVH inherit the plain
  `VpsProvider` discovery (no offline reconstruction), so Trip 1's CRASHED-after-sketchy-kill
  assertion and any offline view cannot hold.
- **No `ProviderUnavailableError` on missing credentials.** Vultr swallows missing creds to a
  silent `[]` + WARN (`build_provider_instance`); OVH never raises the contract error. Trip 4's
  missing-credential assertion would not hold.
- **OVH `destroy_host` is cancel-at-expiration, not delete-now** (`set_renew_at_expiration` in
  `libs/mngr_ovh/imbue/mngr_ovh/client.py`), so Trip 1's "backend is clean after gc" probe needs an
  OVH-specific "scheduled for cancellation" assertion rather than "the VPS is gone".

Until these land, Vultr/OVH cannot meaningfully run the cost-stop, offline-visibility, or
error-classification trips.

### 2. Lima / Docker / SSH are not on the harness

- **Lima** has only `test_lima_btrfs_host_end_to_end_release`
  (`libs/mngr_lima/imbue/mngr_lima/test_lima_btrfs_release.py::test_lima_btrfs_host_end_to_end_release`),
  not a `ProviderReleaseProfile`. Lima has no `auto_shutdown_seconds` (Trip 2 would skip) and no
  `prepare`/`cleanup`, but Trip 1 (lifecycle + sketchy-kill via `limactl delete --force` + gc) and
  Trip 4 (error classification) are applicable and not yet wired up.
- **Docker** (the local provider, `libs/mngr/imbue/mngr/providers/docker/`) has lifecycle / state /
  gc integration tests but no `ProviderReleaseProfile`. Trip 1 (sketchy-kill via `docker rm -f`) is
  applicable.
- **SSH** has no release-trip coverage and known honesty gaps (`supports_shutdown_hosts = True` but
  `stop_host` raises, no offline view) that a Trip 1/Trip 4 profile would surface.

### 3. Trip 1b — second agent on the same host (N agents per host)

Not implemented anywhere. The harness docstring lists it as still owed. It would assert §1.8 of
`specs/provider-shape.md`: add a second agent on a host Trip 1 already provisioned, confirm both are
listed with distinct `agent_id`s and have distinct per-host persisted records
(`list_persisted_agent_data_for_host`), that a stop/start cycle preserves both, that the offline
mirror shows both while the host is stopped, and that destroying one leaves the other. It needs no
new boot — it piggy-backs on the Trip 1 host.

### 4. Offline host_dir read trip

Partial. Trip 1 has an opt-in offline-host_dir read step (gated by the
`MNGR_RELEASE_TEST_OFFLINE_HOST_DIR` env var and `supports_offline_host_dir`, currently AWS/Azure
only), which reads the Trip 1 marker via `mngr file get --relative-to host` while the host is
stopped. The harness docstring still calls for a dedicated end-to-end offline-host_dir trip that
creates with the offline host_dir enabled, writes a file, takes the host offline, and asserts the
file is served from the mirror rather than the live host. The opt-in step covers the core of this;
a standalone always-on trip does not yet exist.

### 5. Capability-flag honesty: `supports_volumes`

Still a real divergence and not pinned. The VPS family reports `supports_volumes = True`
(`VpsProvider.supports_volumes`, `libs/mngr_vps/imbue/mngr_vps/instance.py`) but
`VpsProvider.list_volumes` returns `[]` and `VpsProvider.delete_volume` is a no-op. No trip asserts
the flag's honesty (the harness has no `supports_volumes` branch), and the implementation was not
landed / the flag not flipped. This belongs either in the harness as a volume-honesty step or as a
per-provider capability-flag unit test in `*_test.py`.

### 6. Snapshot-restore (`--snapshot` at create) on the VPS family

Trip 3 asserts the *documented divergence* (`snapshot_survives_destroy = False`): the container
shape's `docker commit` snapshot dies with the VPS, so the harness checks the snapshot record is
gone after destroy. The underlying gap remains — `VpsProvider.create_host` and `VpsProvider.start_host`
accept a `snapshot` / `snapshot_id` argument but never use it (silent no-op). Trip 3 will flip to a
hard portable-restore assertion (set `snapshot_survives_destroy = True`) once the VPS family honors
the argument; that fix has not landed.

### 7. Container-ingress probe (§3.10)

Not in any trip. Proposed addition: probe the container SSH port from an IP outside the test's
`allowed_ssh_cidrs` and expect it refused (it should succeed on Vultr/OVH, which have no managed
firewall, and be refused on AWS/Azure/GCP). Requires a CI-stable source IP excluded from the test
CIDR. Out of scope for the current harness.

---

## Notes for whoever closes these

- The trip body speaks only through the `mngr` CLI + the profile's capability booleans + cloud-API
  probe hooks (`find_launched_host_handle`, `is_host_compute_running/stopped`, `force_strand_host`,
  `is_backend_clean`). Adding a provider means writing a `ProviderReleaseProfile` subclass, not
  touching the harness.
- Use a capability boolean (e.g. `supports_shutdown_hosts`) to branch where a provider documentably
  does not claim a capability; assert the divergence directly where a provider claims a capability
  but the implementation diverges, so the test flips when the fix lands. This is how the
  already-landed findings are encoded.
- Closing a Vultr/OVH gap means fixing the *provider* first (orphan scanner, VM-level stop, offline
  reconstruction, contract error), then attaching the profile.
