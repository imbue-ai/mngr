# Unabridged Changelog - mngr_gcp

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/mngr_gcp/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-23

SSH host keys are now unique per host (inherited from the shared VPS provider): each host gets its own VPS/VM-root and container sshd host keypair at create time rather than sharing one keypair across every host the provider instance created. Pause/resume of hosts created before this change still works via a fallback to the legacy provider-global key.

## 2026-06-22

Report an unauthenticated GCP provider consistently with the other cloud providers.

A missing/unresolvable ADC credential or project now raises the shared `ProviderNotAuthorizedError` (still a `ProviderUnavailableError`, so read paths treat it as unavailable). In `mngr list` this surfaces as one consistent error line and a non-zero exit.

## 2026-06-21

- GCP hosts inherit the shared VPS host-setup fix that registers the gVisor
  (runsc) runtime with `--overlay2=none`, so an agent container's writable layer
  persists across a `docker stop`/`start` or host reboot instead of being lost to
  the default per-sandbox overlay.

## 2026-06-19

The GCP provider's `project_id` config field now defaults to `None` instead of `""`, making "unset" explicit and matching the other optional identifier fields (`default_region`, `default_zone`, `service_account_email`). Resolution behavior is unchanged: an unset `project_id` still falls back to the project Application Default Credentials resolves from the environment.

Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename: the VPS
provider is no longer Docker-only, so the package and its shape-agnostic base
classes dropped "Docker" from their names (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerHostStore` -> `VpsHostStore`, `VpsDockerError` ->
`VpsError`). Import-only change; no behavior difference.


Enabled bare placement (`isolation=NONE`): the idle agent runs `shutdown -P now`
as the VM's root, which on GCE stops the instance, so the container-only sentinel +
host-side systemd watcher is skipped for bare.

Added bare-placement (`isolation=NONE`) release tests, and fixed a resume bug they
caught: `start_host` read the host record via the Docker volume, which a bare host
does not have, so it now resolves the store through the realizer.

``stop_host`` / ``start_host`` moved to the shared base ``OfflineCapableVpsProvider``; GCP now supplies only the GCE ``_pause_cloud_instance`` / ``_resume_cloud_instance`` hooks. Behavior-preserving.

Moved mngr host identity (host id and created-at) out of GCE *labels* and into instance *metadata*, joining the host name and per-agent records already kept there. Only ``mngr-provider`` remains a label, because it is the server-side ``instances.list`` discovery filter. Host id is now stored verbatim and created-at as an ISO-8601 timestamp (no more GCE-charset lowercasing / ``%Y-%m-%dt%H-%M-%S`` encoding). Backward-incompatibility: a GCE instance created before this change carries its host id / created-at only in labels, so an *already-running* pre-upgrade host will no longer resolve by id for offline discovery / ``mngr start`` and its reconstructed created-at falls back to now(); destroy and recreate such hosts (online hosts reachable over SSH are unaffected -- they resolve via the on-volume records).

The idle-watcher install (in-container sentinel `shutdown.sh` plus the host-side systemd `.path`/`.service`) and the best-effort `_on_host_finalized` step runner moved to the shared `OfflineCapableVpsProvider`. GCP now supplies only the `GCE instance` display name; its `.service` body is the shared default `shutdown -P now` (a GCE guest poweroff stops the instance) and it does not sync host_dir to an object store, so it inherits the no-op sync gate and installs no sync daemon. The host-side idle-watcher systemd unit name changed from `mngr-gcp-idle-watcher` to the shared `mngr-idle-watcher`. Behavior-preserving otherwise.

Updated the VPS build-arg parsing imports to point at the new `imbue.mngr_vps.build_args` module (moved out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

Updated the `OfflineCapableVpsProvider` import to the new `imbue.mngr_vps.instance_offline` module (split out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

`GcpProvider` now extends the new shared `KeyValueMirrorVpsProvider`, which owns the offline read-side reconstruction over a key-value mirror (previously duplicated between GCP's metadata code and the AWS/Azure tag code). GCP supplies only the metadata-map hook (`_offline_kv_map`) and the host-name key (`_host_name_key`); the per-agent metadata *write* side (the single `setMetadata` round-trip) is unchanged, and GCP inherits no object-store/bucket machinery. The GCP-local `_agent_metadata_items` / `_agent_metadata_value` / `_persisted_agent_dicts_from_instance` / host/created-at reconstruction helpers collapse into the shared base. Behavior-preserving.

`mngr gcp prepare` / `cleanup` now resolve their `[providers.<name>]` block and refuse-on-existing-instances via the shared `mngr_vps.cli_helpers`, and `GcpProviderConfig` lifts `allowed_ssh_cidrs` into a shared config base (it keeps its own `associate_external_ip`, which GCP names differently from AWS/Azure's `associate_public_ip`) instead of carrying GCP-local copies. The cleanup refusal when instances still exist now raises the unified `ManagedResourcesExistError` (previously `GcpError`) so the message matches the other clouds. `allowed_ssh_cidrs` is now typed `ScalarStrTuple` (matching AWS) rather than a plain tuple, so a higher-precedence config layer that sets it replaces the whole list rather than being flagged as narrowing; the config key and default are unchanged.

Further internal dedup against the shared offline layer (no user-visible behavior change): `_list_provider_vps_hostnames` is now inherited from the shared `KeyValueMirrorVpsProvider` (cached listing -> non-empty `main_ip`), and `_create_vps_instance` uses the new shared `_require_parsed` helper in place of its hand-written `match`/type-narrowing guard. GCP still inherits no bucket/tag-store machinery.

Corrected the README "Implementation details" to match the label-vs-metadata move: only `mngr-provider` is a GCE label; `mngr-host-id` (incl. the stopped-host lookup for `mngr start`), `mngr-host-name`, and `mngr-created-at` live in instance metadata.

Integrated the `mngr/volumes` offline-store simplification (commit `f8bb5c0a5`): the per-agent instance-tag mirror is removed in favor of a single uniform external `HostStateStore` per provider -- AWS/Azure use their object-storage state bucket as the sole offline store (a stopped host's offline metadata now requires the bucket; the provider's `_state_store` raises an actionable `missing_state_bucket_error` pointing at `mngr <cloud> prepare` when the bucket is absent), and GCP uses a lossless instance-metadata-backed store (full host record + one JSON value per agent). AWS/Azure/GCP now extend `OfflineCapableVpsProvider` directly. This supersedes the earlier-on-this-branch tag-mirror dedup (the lifted `TagHostStateStore` / `KeyValueMirrorVpsProvider` / `TagMirrorVpsProvider` are gone); the realizer architecture, the systemd-unit hardening, and the cli/config/state-bucket dedup are retained. No behavior change for container hosts beyond the offline-metadata-requires-bucket consequence noted above.

Bugfix: a running bare (`isolation=NONE`) host is now discoverable and reachable
with the default provider config -- `mngr conn`/`list`/`stop`/`start`/`destroy`
no longer need `-S providers.<name>.isolation=NONE` at connect time. GCE instances
now carry a `mngr-isolation` value in instance metadata (where GCP keeps mngr
identity; GCE labels are too restricted), stamped at create, so discovery reads
the host's placement from the cloud API without SSH and probes it with the
matching realizer. Pre-existing instances have no marker and default to container,
preserving prior behavior.

Bugfix: renaming a host now re-stamps the `mngr-host-name` instance metadata (the
cheap identity tag offline discovery reads), not just the host record. Previously
this metadata was written only at create, so a host that was renamed and then
stopped still listed under its old name in offline discovery; it now lists under
its new name.

Internal dedup (no behavior change): GCP host-name recovery from instance metadata
now calls the shared `host_name_from_prefixed_value` helper instead of a private copy
of the strip-prefix / host-id fallback logic.

Added offline ``host_dir`` support for the GCP provider, matching the AWS / Azure shape. A stopped GCE instance's ``host_dir`` is now readable without starting it (so ``mngr event`` / ``mngr transcript`` / ``mngr file`` work against it), captured operator-side at ``mngr stop`` and uploaded to a Google Cloud Storage state bucket. Host + agent records still live in GCE instance metadata (where they already fit comfortably and need no prepare step).

``mngr gcp prepare`` now also creates a GCS state bucket (named ``mngr-state-<project_id>`` by default, configurable via ``[providers.gcp] state_bucket_name``). ``mngr gcp cleanup`` now deletes that bucket alongside the firewall rule, with a new ``--force`` flag that opts into deleting it even when it still holds offline host state from hosts no longer present as instances.

New config fields on ``GcpProviderConfig``: ``state_bucket_name`` (overrides the derived name) and ``is_offline_host_dir_enabled`` (default on; set to ``False`` to turn the feature off without removing the bucket).

The shared provider release harness's Trip 1 opt-in offline-host_dir step (gated by ``MNGR_RELEASE_TEST_OFFLINE_HOST_DIR=1``) now runs against GCP too, asserting that a stopped host's marker file is served from the offline mirror via ``mngr file get`` without resuming the host.

Trimmed the README to user-relevant content (removed internal implementation details, release-test instructions, and roadmap notes) and tightened it for concision.

Aligned the GCP provider config field descriptions (surfaced via `mngr config`/help) with the README's "GCP-specific configuration" table, and corrected the `auto_shutdown_seconds` README row (the VM halts via `shutdown -P`, it does not self-delete).

Fact-checked the README against the `mngr_vps` base module and the nullable `project_id` default.

Added `test_provider_release_trip1` to the GCP release suite: a single-boot full-lifecycle trip (create, exec, stop, real `--stop-host`, start, persistence, snapshot, out-of-band kill, gc, backend-clean) parametrized over container and bare isolation, built on the shared provider release harness. Also added `test_provider_release_trip3` (snapshot survives destroy); on GCP the docker-commit snapshot is not portable, so the trip asserts that documented divergence (the snapshot is gone after destroy).

Retired the old per-step GCP lifecycle release tests now that the trips supersede them: `test_provider_lifecycle_create_exec_and_destroy`, `test_provider_lifecycle_create_stop_start_destroy`, and `test_bare_provider_lifecycle_create_exec_and_destroy`. The bare-shape check the bare test owned (the agent shell is the VM's own root -- `/var/lib/mngr-host` present, no `/.dockerenv`) now runs inside Trip 1 for the NONE-isolation parametrization.

Also added `test_provider_release_trip4` (error classification): a no-boot CLI trip asserting `mngr create` with unresolvable GCP ADC surfaces the contract `ProviderUnavailableError`, and that a `--vps-*` build arg is rejected with the migration hint. This PR also fixes the GCP missing-credential help text to point at `gcloud auth application-default login` (and the project/ADC setup) instead of the generic "start Docker" guidance; the trip asserts that curated help.

Also added `test_provider_release_trip2` (idle auto-shutdown contract), parametrized over container and bare isolation: it creates an idle host, polls until the GCE instance is TERMINATED (billing stops, disk preserved), then resumes via `mngr start` and asserts a pre-shutdown marker survived.

## 2026-06-18

GCP's offline host/agent store now holds the *full* host record instead of a lossy field subset: a stopped GCE instance's `mngr list` / `mngr start` reconstructs the complete record (config, IP, host keys), matching the AWS/Azure behavior, rather than the previous minimal label-only reconstruction. The full `VpsDockerHostRecord` JSON is stored in the `mngr-host-state` instance-metadata value and each agent record in a single `mngr-agent-<id>` metadata value, replacing the per-field `mngr-agent-<id>-<name|type|labels>` layout and the `mngr-created-at`-label reconstruction. GCE instance metadata is large and permissive enough (256 KB per value, 512 KB per instance) to hold these records, so GCP needs no separate object-storage bucket.

GCP's offline store is now exposed through the same `HostStateStore` interface as the AWS/Azure object-storage buckets (a `_GceMetadataHostStateStore`), so its offline read/write/discovery paths are shared with the other providers.

## 2026-06-17

Added native GCE stop/start lifecycle (idle-pause + resume) for GCP hosts: `mngr stop` now stops the GCE instance (preserving the boot disk so a paused agent costs only disk storage) and `mngr start` resumes it, reading back the fresh external IP and rebinding known_hosts. Stopped instances stay discoverable -- their host name and per-agent records are mirrored into instance metadata, and labels carry the host id / created-at -- so `mngr list` and `mngr start <agent>` keep working while a host is TERMINATED or mid-stop. An in-container idle watcher self-stops the instance via a host-side systemd path/service unit.

Internal: GCP's stopped-host offline discovery and resolution (listing TERMINATED / mid-stop hosts, resolving them by id, and falling back to instance metadata), plus its stop/start lifecycle, known_hosts rebinding, and idle-watcher install, now come from a shared `OfflineCapableVpsDockerProvider` base instead of GCP-specific copies; GCP supplies only the GCE-specific hooks (stop/start the instance, label-encoded host-id match, poweroff idle action). No behavior change.

## 2026-06-16

## GCP provider

- The GCP release-test settings now also disable the `azure` provider (`[providers.azure] is_enabled = false`), mirroring the existing modal/aws/vultr/ovh disables. Without it, `mngr list` inside the GCP lifecycle tests would enumerate the newly-added azure provider and exit non-zero when Azure credentials weren't resolvable in that subprocess, failing the GCP tests for a non-GCP reason.

- `mngr gcp prepare` / `mngr gcp cleanup` group their GCP-specific options under a "Provider" option group, so `--help` and the generated docs list them ahead of the shared common options instead of below them.

Removed the dead VPS client methods `create_snapshot`, `delete_snapshot`, `list_snapshots`, and `list_ssh_keys` (and the now-unused `_boot_disk_source` helper and snapshots compute client) from `GcpVpsClient`. These had no production callers and are being dropped from the shared `VpsClientInterface`. The corresponding unit and release tests, plus the `FakeSnapshotsClient` test helper, were removed as well.


The `mngr_gcp` README's snapshot note now states the GCP client exposes no disk-snapshot surface (rather than naming the removed `create_snapshot` / `list_snapshots` / `delete_snapshot` methods).

## 2026-06-15

## GCP Compute Engine provider

- New `gcp` provider backend (`mngr_gcp`) that runs agents in Docker containers on Google Compute Engine VMs. Built on the shared `mngr_vps_docker` base, exactly like the AWS EC2 provider.
- Credentials are resolved exclusively via Google Application Default Credentials (`google.auth.default()` — `GOOGLE_APPLICATION_CREDENTIALS`, `gcloud auth application-default login`, attached service account / metadata server). `[providers.gcp]` config has no credential fields, matching the Modal and AWS provider convention. Only the non-secret `project_id` (required) and optional `service_account_email` / `service_account_scopes` identifiers are configured.
- Privileged firewall setup is split into a one-time `mngr gcp prepare` operator command (mirroring `mngr aws prepare`): it creates a network-scoped, tag-targeted rule (`mngr-gcp-ssh` by default) opening tcp/22 and the container SSH port to every CIDR in `--allowed-ssh-cidr`. The hot `mngr create` path only resolves the rule read-only and errors with a pointer to `prepare` if missing, so developers can create with instance-only IAM (no `compute.firewalls.create`).
- `mngr gcp prepare` and `mngr gcp cleanup` now read their defaults from the user's `[providers.<name>]` settings.toml block (selected with `--provider`, default `gcp`), matching `mngr aws prepare`. Previously they used `GcpProviderConfig` class defaults unconditionally, so a user who pinned a non-default `default_zone` / `network` / `firewall_name` / `allowed_ssh_cidrs` in their config and ran `prepare` without the matching CLI flag would create the firewall rule with class defaults while the runtime `mngr create` path used their configured values -- e.g. landing the rule on the wrong network. CLI flags still override the resolved config, which overrides class defaults. A warning is logged if the named `--provider` block exists but is not a GCP backend.

- `allowed_ssh_cidrs` now defaults to `0.0.0.0/0` and is fail-open, matching the AWS provider (previously it defaulted to empty and fail-closed -- `prepare` refused, and the auto-firewall path raised, without an explicit CIDR). `mngr gcp prepare` with no `--allowed-ssh-cidr` now falls back to that default and creates a wide-open rule, logging a warning prompting you to tighten it for production (a `0.0.0.0/0` range is also warned at create time). Setting `allowed_ssh_cidrs = []` opts out entirely: no firewall rule is created (GCE rejects an empty-source rule) and the instance launches unreachable from outside its VPC, the closest analog to AWS's "empty security group, no ingress" behavior.
- SSH key injection uses per-instance `ssh-keys` metadata (`ubuntu:<pub>`) plus a direct write into root's authorized_keys by the bootstrap; OS Login and project-wide keys are disabled per instance (`enable-oslogin=FALSE`, `block-project-ssh-keys=TRUE`). The first-boot bootstrap is delivered via the GCE `startup-script` metadata key (run by the google-guest-agent on every image), not cloud-init `user-data`, which stock GCE Debian images ignore.
- GCE-native auto-delete safety net: when `auto_shutdown_seconds` is set, instances are launched with `scheduling.max_run_duration` + `instance_termination_action=DELETE`, so the VM self-deletes even if the orchestrating process is killed (the true analog of AWS `InstanceInitiatedShutdownBehavior=terminate`). The unit is seconds (shared with the AWS provider via the base `VpsDockerProviderConfig`); GCE's `max_run_duration` is seconds-native, so the value is passed through directly.
- Instances are labeled `mngr-provider`, `mngr-host-id`, and `mngr-created-at`; discovery filters `instances.list` by the `mngr-provider` label. Network tags target the firewall rule.
- GCE image families are global (no per-region map): `default_source_image` (the GCE VM image, distinct from the base `default_image` which is the Docker container image) defaults to `projects/debian-cloud/global/images/family/debian-12`, matching the rest of the mngr fleet (Vultr/OVH/AWS all run Debian 12 bookworm). Stock GCE Debian images run the `google-guest-agent` rather than cloud-init, so GCP bootstraps via the GCE `startup-script` metadata (which the guest agent runs on every image) instead of cloud-init `user-data`; this is what lets Debian -- and any image, regardless of whether it ships cloud-init -- work. (The shared `mngr_vps_docker` Docker install derives the apt repo + pinned version from `/etc/os-release` at run time, so the same step works across Debian-family images.)
- Disk-snapshot operations (`GcpVpsClient.create_snapshot` / `delete_snapshot` / `list_snapshots`) are intentionally unwired: they raise `VpsDockerError` ("disk snapshot support is not implemented in mngr_gcp") rather than calling the Compute Engine snapshot API, matching the AWS provider. The VPS-client-level snapshot surface has no production callers (host snapshots go through `docker commit` at the provider layer), so the live `SnapshotsClient` wiring and its snapshot IAM permissions were dropped.
- `project_id` is no longer hard-required: when it is unset, the GCP provider falls back to the project that Application Default Credentials resolve from the environment -- the active `gcloud config set project` or the `GOOGLE_CLOUD_PROJECT` env var. An explicitly configured `project_id` still wins, and `mngr create` logs which project it inferred when relying on the fallback (so a stray gcloud default never silently bills an unexpected project). The fallback reuses the single `google.auth.default()` call already made for credentials, so there is no extra probe on the common path.
- When neither a configured `project_id` nor an ADC-resolved project is available, the error now points at the exact fixes: `mngr config set providers.gcp.project_id <your-project>`, `GOOGLE_CLOUD_PROJECT`, or `gcloud config set project` -- instead of describing the settings.toml edit by hand.
- Smoother first-run onboarding: the missing-firewall check now runs as a read-only pre-flight in the pre-create hook, *before* any provider write (SSH key upload / instance creation). A first-time user who hasn't run `mngr gcp prepare` gets the clean, actionable "run `mngr gcp prepare`" message immediately, instead of it surfacing mid-create under a `Host creation failed, attempting cleanup...` line. (The hot `create_instance` path still resolves the rule too; the extra read is cheap.)
- New `mngr gcp cleanup` CLI command, the safe inverse of `prepare`: it deletes the `mngr-gcp-ssh` firewall rule so a project returns to its pre-`prepare` state (handy when retiring a provider or testing the first-run experience). It refuses (deletes nothing) while any mngr-managed instance still exists anywhere in the project -- checked across all zones via an aggregated list, because the firewall rule is network-global, so it can never strand a running agent's SSH access. Idempotent (a no-op when the rule is already gone). Needs `compute.instances.list` (aggregated) + `compute.firewalls.get` + `compute.firewalls.delete`. Backed by new `GcpVpsClient.delete_firewall()` and `list_mngr_managed_instances()`. Mirrors `mngr aws cleanup`.
- Release tests double-gated by `MNGR_GCP_RELEASE_TESTS=1` plus ADC presence; a `pytest_sessionfinish` hook in `libs/mngr_gcp/imbue/mngr_gcp/conftest.py` scans for any test-tagged GCE instance older than the TTL at session end, force-deletes leaks, and fails the session.

- A misconfigured GCP provider (no resolvable ADC credentials, or no project pinned and none resolvable from the environment) is no longer silently dropped from `mngr list` / `mngr connect` / `mngr gc`. A credential/project resolution failure means GCP was never reached, so the provider's state is *unknown* (there may be running hosts that simply can't be seen). The backend now raises `ProviderUnavailableError` for this case instead of `ProviderEmptyError`: the shared discovery path surfaces it to the user rather than treating the provider as definitively empty, and `mngr gc` skips it instead of treating an unreachable provider's hosts as garbage, per the documented contract of the two error types. (`mngr create --provider gcp` continues to surface the same actionable error directly.)

- README now documents that `mngr stop` / `mngr start` are container-level, not VM-level: they stop/start the agent's Docker container over SSH, while the GCE VM keeps running and billing. VM-level stop/start (pausing the GCE instance to stop compute billing) is called out as a known limitation / future work; it is not provided by the AWS-only `mngr/aws-stop` branch and would need a parallel `mngr_gcp` implementation.

- New per-host `--gcp-spot` build arg (`mngr create --provider gcp -b --gcp-spot`) launches the agent on GCE Spot capacity (`scheduling.provisioning_model=SPOT`, with `instance_termination_action=DELETE` so a preempted Spot VM is deleted rather than left stopped). It composes with `auto_shutdown_seconds`. GCE can preempt Spot VMs at any time (~30s notice), so it is opt-in only -- good for ephemeral / experimental agents, risky for long-lived ones. Mirrors the AWS `--aws-spot` flag. Note: the `SPOT` + `max_run_duration` combination has not yet been exercised against a live GCE create (no spot release test); validate before relying on the two together.

- `mngr gcp prepare` and `mngr gcp cleanup` now respect `--format`. Previously they ignored it: success was logged to stderr and the bare firewall-rule name was echoed to stdout regardless of format. They now emit a single result line in `human` mode, a structured object in `json` mode (`{firewall_name, target_tag, project_id, created}` for prepare; `{firewall_name, project_id, deleted}` for cleanup), and a `prepared` / `cleaned_up` event in `jsonl` mode. The `created` / `deleted` booleans let a caller distinguish a first-run create from an idempotent no-op. The redundant bare-name stdout line in human mode is gone.

- The wide-open-CIDR warning is shorter: `mngr gcp prepare` with `0.0.0.0/0` ingress now logs just "auto-created firewall rule '<name>' will permit SSH from the public internet." (the trailing dev-vs-production advice sentence was dropped).

- Internal hardening from review: GCE label values and instance names are now modeled as validated `NonEmptyStr` subtypes (`GceLabelValue` / `GceInstanceName`) rather than bare `str`, so the coercion output is re-asserted valid at its point of use (matching the codebase's `SnapshotId` / `SafeName` identifier-typing convention) and a pathological empty/invalid coercion fails fast instead of shipping an invalid identifier to GCE.

- The `GcpVpsClient.image` field is now optional (`None` default): the `mngr gcp prepare` / `cleanup` operator commands build the client image-less (they only touch firewall rules and never launch an instance), instead of passing a misleading placeholder image. `create_instance` raises a clear error if asked to create without an image.

- `mngr gcp prepare` / `cleanup` now resolve and validate the target project *before* constructing the client (raising `GcpProjectError` when none resolves), so the client always holds a real project rather than an empty-string placeholder threaded through every API call.

- The operator CLI now raises the provider's domain error types (`GcpError` / `GcpProjectError` / `GcpCredentialsError`) instead of bare `click.ClickException`, consistent with the rest of the GCP code (these are all `MngrError` subclasses, so the rendered CLI message is unchanged).

- Doc/test cleanups: firewall-rule descriptions no longer call the rule "auto-created" (it is created by `mngr gcp prepare`, not mid-create); dropped a change-detector config-defaults test and a redundant `resolve_project_id` test.

- `default_zone` / `default_region` are no longer hardcoded: both now default to unset (`None`). When `default_zone` is unset the zone is taken from the active `gcloud config get compute/zone` (best-effort -- skipped cleanly when the gcloud CLI is absent, exactly as `google.auth` already consults gcloud for the default project), falling back to `us-west1-a`. The region is derived from the resolved zone unless `default_region` is set explicitly, in which case a zone/region mismatch is still rejected as a likely typo. An explicit `default_zone` and the `--gcp-zone=` / `--zone` flags continue to win. This applies to both the runtime `mngr create` path and the `mngr gcp prepare` / `cleanup` operator commands, so a user's gcloud zone flows through everywhere without per-provider config.

- New per-host `--gcp-image=<image>` build arg (`mngr create --provider gcp -b --gcp-image=...`) boots a single VM from the given GCE source image (a full image or family URL) instead of the config's `default_source_image`. Unlike the other VPS providers, where image selection is config-only, GCP exposes this per-host knob; an unset flag falls back to `default_source_image`. Any image works (the `startup-script` bootstrap is run by the guest agent regardless of whether the image ships cloud-init).

- Clarified that an unset `service_account_email` (the default) omits the field from the create request, so GCE applies its normal default for an unspecified service account (rather than attaching no service account). This is a doc-only clarification; the behavior is unchanged.

- Removed the unexplained `time.sleep(20)` settle-cushion after `mngr destroy --force` in the two lifecycle release tests (`test_release_gcp.py`). The sleep was the last statement of each test and masked no race: `destroy_instance` already blocks on the GCE delete operation (`operation.result()`), and nothing runs after the sleep except the TTL-gated session-end leak scanner. The `time_sleep` ratchet for this project is tightened 2 -> 0 accordingly.

- A `startup-script` runs after sshd has already booted with a freshly-generated host key (it has no pre-sshd hook, unlike cloud-init's `ssh_keys`), so the script installs mngr's host key and restarts sshd as its first action, and the provisioner polls the VM's live SSH host key until it matches the expected one before opening the strict-host-key-checked connection. This closes the host-key-mismatch window without weakening the no-TOFU guarantee (only the exact expected key is accepted).
