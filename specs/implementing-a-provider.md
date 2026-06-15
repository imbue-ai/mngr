# Implementing a new `mngr` provider

High-level guide for adding a new provider plugin. Use alongside `specs/provider-shape.md` (the prescriptive contract — read first), `specs/provider-uniformity-review.md` (current-state cross-provider behavior), and `specs/provider-release-tests.md` (release-test trips).

The guide is organized around the **user-visible behaviors** a provider must deliver. Each section names the behavior, the contract the user expects, and where to put the code. Backend-shape specifics — cloud VPS vs hosted sandbox vs local — only matter for "where the code lives," not for the contract.

## Before you start

A provider is a pluggable backend that allocates compute, runs an agent on it, and lets the user `mngr exec`, `mngr list`, `mngr stop`, `mngr start`, `mngr destroy`, and (if it has per-user backend resources) `mngr <yourname> prepare` / `cleanup`. The single most important user expectation is that `mngr` feels the same across providers. Where uniformity is impossible, be loud about the gap (raise, or flip a capability flag); silent no-op is the worst option.

Three common backend shapes, each with a reference implementation:

- Cloud VPS / VM (Debian + Docker on a public-IP VM). Subclass `VpsDockerProvider`. Reference: `libs/mngr_aws/imbue/mngr_aws/`.
- Hosted sandbox (provider-managed compute, no VM lifecycle exposed). Implement `ProviderInstanceInterface` directly. Reference: `libs/mngr_modal/imbue/mngr_modal/`.
- Local / BYO. Reference: Lima, Docker, SSH providers in-tree.

For each behavior below, the contract is identical; the implementation hooks differ by shape.

## Deliver: `mngr create`

User contract: provisions a host, starts one agent (unless `--no-agent`), leaves the user able to `mngr exec` into it. Build args validated; unknown / migration-flag rejected loudly. Pre-create gate refuses if a required prerequisite is missing (operator hasn't run `prepare`; pytest cost-safety not configured).

Where to put the code (VPS shape): `_parse_build_args` (compose `parse_vps_build_args(provider_prefix="--<yourname>-")` + the `extract_*` helpers; reject unknown via `raise_if_unknown_provider_arg`; reject migration flags via `raise_if_vps_migration_arg`); `_create_vps_instance`; `_validate_provider_args_for_create` (model: `libs/mngr_gcp/imbue/mngr_gcp/backend.py` — firewall preflight + project-resolution warning + pytest gate).

Where to put the code (sandbox shape): `create_host` directly. Modal does build/snapshot wiring + Volume-backed host record in `libs/mngr_modal/imbue/mngr_modal/instance.py`.

Contract spec: `provider-shape.md` §1.1.

## Deliver: `mngr list`

User contract: shows every host the user has created, in every state — RUNNING, STOPPED, CRASHED, DESTROYED (with `--include-destroyed`). Credentials missing raises `ProviderUnavailableError`, NOT a silent empty list. Per-command API hit; cached for the duration of one command.

Where to put the code (VPS shape): `_fetch_provider_instances` returning instance dicts filtered to `mngr-provider=<self.name>`; `_list_provider_vps_hostnames` returning SSH-reachable hostnames. The shared discovery flow (SSH-into-each-VPS, offline fallback) lives in `VpsDockerProvider`.

Stopped-host visibility requires an offline mirror — AWS's `_discovered_host_from_tags` + `_offline_host_from_tags` rebuild stopped hosts from EC2 tags. Without it, a stopped VM falls out of `mngr list`.

Contract spec: `provider-shape.md` §1.2.

## Deliver: `mngr stop` and `mngr stop --stop-host`

User contract for `mngr stop` (no flag): stops the agent's tmux session only. Compute keeps running. Uniform across all providers — this is at the API layer, not your provider.

User contract for `mngr stop --stop-host`: either (a) stop compute so the user stops paying, OR (b) refuse loudly via `HostShutdownNotSupportedError`. Silent leave-VM-running while reporting "Stopped host" is a cost leak masquerading as success — and it's what Azure/GCP/Vultr/OVH do today by inheriting the base unchanged.

Where to put the code: override `stop_host` if your provider has VM-level stop and you want `supports_shutdown_hosts=True`. AWS pattern: `libs/mngr_aws/imbue/mngr_aws/backend.py` — `StopInstances`, EBS preserved, `stop_reason=STOPPED` written via base. If you can't honestly stop compute, set `supports_shutdown_hosts=False` and let the CLI refuse before the work begins.

Contract spec: `provider-shape.md` §1.3, §1.4.

## Deliver: `mngr start`

User contract: idempotent; resumes a stopped host. If `--snapshot <id>` was passed, either restore from it or raise `SnapshotsNotSupportedError`. Silent no-op (current VPS-family behavior on `snapshot_id`) is the worst option.

Where to put the code: override `start_host` if your provider has VM-level stop. AWS additionally re-binds known_hosts for the new public IP after resume and re-launches the in-container activity watcher.

Contract spec: `provider-shape.md` §1.5.

## Deliver: `mngr destroy` and `mngr <provider> cleanup`

User contract for `mngr destroy`: deletes every billable resource attached to the host. Idempotent on 404. Raises `CleanupFailedGroup` if any real resource was left behind, so the user sees the punch list. May preserve snapshots; if so, `gc_snapshots` handles them.

Where to put the code: the shared `destroy_host` in `VpsDockerProvider` covers most VPS shapes via cloud-native cascades (`DeleteOnTermination`, `delete_option=Delete`, `auto_delete=True`). Your client's `destroy_instance` is the one new method.

User contract for `mngr <provider> cleanup`: only if your provider creates per-user backend resources (security group, firewall rule, IAM role). Inverse of `prepare`; refuses while user resources exist; tag-scoped (never deletes infrastructure lacking a `mngr-*` tag). Register via the `register_cli_commands` hookimpl. If your provider has no per-user resources (Modal, local), skip this — don't add a no-op for parity.

Contract spec: `provider-shape.md` §1.6, §1.7.

## Deliver: capability flags

`supports_snapshots`, `supports_shutdown_hosts`, `supports_volumes`, `supports_mutable_tags`. These are honesty contracts the CLI branches on. `True` means the method does what users expect; `False` means it raises clearly. `True` with a no-op implementation is the worst option.

Three current lies to avoid: SSH's `supports_shutdown_hosts=True` while `stop_host` raises `NotImplementedError`; VPS-family `supports_volumes=True` while `list_volumes()` returns `[]`; cloud-trio `supports_snapshots=True` while snapshots don't survive `destroy_host` (a `docker commit`, not a portable snapshot).

Contract spec: `provider-shape.md` §2.

## Deliver: error classification

User contract: every failure mode classifies into the right exception:

- Cloud creds missing / API down → `ProviderUnavailableError` with curated `user_help_text`. Default text says "start Docker" — wrong for cloud auth. Pattern: `_azure_unavailable_error` in `libs/mngr_azure/imbue/mngr_azure/backend.py`.
- Backend reachable, zero hosts → `ProviderEmptyError`. Used only when the backend has authoritatively confirmed empty (Modal: "the per-user environment doesn't exist yet").
- Host name doesn't resolve → `HostNotFoundError`.
- Operation requires capability the provider lacks → the specific error (`HostShutdownNotSupportedError`, `SnapshotsNotSupportedError`).
- Multi-resource cleanup partial failure → `CleanupFailedGroup`.

Common error path mistakes today: Vultr/OVH silently return `[]` for missing creds (should raise `ProviderUnavailableError`); Modal raises `ModalAuthError` (a `PluginMngrError`, doesn't satisfy the contract); AWS/GCP fall through to default help text.

Contract spec: `provider-shape.md` §1.9, §5.

## Deliver: N agents per host

User contract: a second `mngr exec <host> --new-agent` succeeds; both agents survive `mngr stop` / `mngr start`; `mngr list` shows both. The interface is `(host_id, agent_id)`-keyed: `persist_agent_data`, `list_persisted_agent_data_for_host`, `remove_persisted_agent_data`. Per-agent storage MUST be keyed per-agent (no single-blob packing).

Where to put the code: live discovery is uniform across the VPS family via the in-container scan at `libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py:1506-1565`. Offline mirror (showing N agents while VM is stopped) is provider-specific — AWS uses per-field tags (`mngr-agent-<id>-name` / `-type` / `-labels`). If your cloud has a tag-count limit, surface it with a clear `NotImplementedError` at the cap.

Contract spec: `provider-shape.md` §1.8.

## Deliver: cost safety

Cost leaks are the most expensive bug class. The user contract: `auto_shutdown_seconds` actually stops billing; idle hosts self-stop if `supports_shutdown_hosts=True`; pytest can't leak resources.

Three mechanisms, all roughly required:

- Pytest gate: `_validate_provider_args_for_create` raises when `PYTEST_CURRENT_TEST` is set and `auto_shutdown_seconds` isn't. Model: `libs/mngr_aws/imbue/mngr_aws/backend.py:200-225`.
- Orphan scanner: `pytest_sessionfinish` in `conftest.py` force-deletes `mngr-pytest-launched=true` resources older than a TTL. Model: `libs/mngr_aws/imbue/mngr_aws/conftest.py:134-180`. Vultr and OVH skipped this and leak real VPSes.
- `auto_shutdown_seconds` actually terminates. Cloud-init `shutdown -P` halts the OS. On AWS that triggers `InitiatedShutdownBehavior=terminate`; on Azure it leaves the VM "Stopped (not deallocated)" and the meter keeps running. Verify with a cloud-API probe in a release test, not just the pre-create gate.

Idle watcher (if `supports_shutdown_hosts=True`): AWS pattern is in-container watcher writing a sentinel; outer-host systemd `.path` unit fires `aws ec2 stop-instances`. Azure/GCP currently have none.

Contract spec: `provider-shape.md` §3.3.

## Deliver: shared defaults

The cross-provider conventions the user relies on:

- `default_idle_timeout = 800` seconds.
- 30 GB default disk.
- `allowed_ssh_cidrs = ("0.0.0.0/0",)` with a runtime warning (key-only SSH is the actual control).
- `debian:bookworm-slim` default container image; pin a specific OS image SKU.
- Tag every resource with `mngr-host-id`, `mngr-provider`, `mngr-created-at`, `mngr-pytest-launched`. Dashes, not underscores (Modal uses underscores; don't copy that).
- Per-host SSH key stored under `<profile>/providers/<yourname>/<instance-name>/keys/`.
- Container ports never exposed directly on `0.0.0.0` of a public IP without the cloud firewall in front.

Contract spec: `provider-shape.md` §3.

## Tests

- Unit tests: config parsing; build-arg parsing (happy path + unknown-flag + `--vps-*` migration); capability-flag pinning; credentials-error classification; cross-region refusal; networking warnings; `auto_shutdown_seconds` flowing through to the cloud API.
- Release tests (`test_release_<yourname>.py`, `@pytest.mark.release`): follow the trip structure in `specs/provider-release-tests.md`. Trip 1 = lifecycle + sketchy-kill + gc; Trip 1b = second agent; Trip 2 = auto-shutdown; Trip 3 = snapshot survives destroy; Trip 4 = error classification.
- Mock fidelity: stub at your client class's surface, not at the cloud SDK. Pattern: `_FakeEc2Client` in `libs/mngr_aws/imbue/mngr_aws/testing.py`.

## Documentation

`libs/mngr_<yourname>/README.md`: Setup (credentials), Build args, RBAC/IAM scopes for `prepare` / `create` / `cleanup`, Multi-region behavior, Defaults, Caveats (anywhere you diverge from the shape doc — be explicit).

Changelog: `libs/mngr_<yourname>/changelog/<branch-name>.md` (slashes → dashes). CI fails without it.

## Common gotchas

- `boto3.Session(region_name=self.default_region)` silently overrides `AWS_REGION`. Defer to env first.
- Disk-size field naming varies (`root_volume_size_gb` / `os_disk_size_gb` / `boot_disk_size_gb`). Use the cloud's own term.
- `start_host(snapshot_id=…)` and `create_host(snapshot=…)` are silently ignored everywhere except Modal and Docker. Honor or raise `SnapshotsNotSupportedError`.
- Vultr/OVH have no managed firewall and no `allowed_ssh_cidrs` field — VPS is public-internet-reachable as soon as it boots. New cloud providers should ship managed-firewall integration.
- AWS per-agent tag mirror hits the EC2 50-tag wall around 16 agents and raises `NotImplementedError`. If your cloud has a similar limit, surface it the same way.

## References

- `specs/provider-shape.md` — the contract.
- `specs/provider-uniformity-review.md` — current-state cross-provider behavior.
- `specs/provider-release-tests.md` — release-test trip proposal.
- `libs/mngr_aws/imbue/mngr_aws/` — reference cloud-VPS provider.
- `libs/mngr_modal/imbue/mngr_modal/` — reference hosted-sandbox provider.
- `libs/mngr_vps_docker/` — shared base.
