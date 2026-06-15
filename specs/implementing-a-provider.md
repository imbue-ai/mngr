# Implementing a new `mngr` provider

High-level guide for adding a new provider plugin. Use alongside `specs/provider-shape.md` (the prescriptive contract — read first) and `specs/provider-uniformity-review.md` (current-state cross-provider behavior). Details that are documented elsewhere are linked, not repeated.

## Before you start

A provider is a pluggable backend that allocates compute, runs a Docker container (or sandbox) on it, talks to it via SSH, and tears it down. Users invoke it through `mngr create -p <yourname>`, `mngr list`, `mngr stop`, `mngr start`, `mngr destroy`, and `mngr <yourname> prepare` / `cleanup`.

The single most important user expectation is that `mngr` feels the same across providers. Where uniformity is impossible, the provider must be loud about the gap (raise, or flip a capability flag); silent no-op is the worst option.

Most new providers fall into one of:

- **Cloud VPS / VM.** Subclass `VpsDockerProvider`. Reference: `libs/mngr_aws/imbue/mngr_aws/`.
- **Hosted sandbox.** Implement `ProviderInstanceInterface` directly. Reference: `libs/mngr_modal/imbue/mngr_modal/`.
- **Local / BYO.** Already covered by Lima / Docker / SSH; you probably aren't writing one.

Taxonomy: `provider-shape.md` §9.

The rest of this guide assumes cloud VPS. For hosted-sandbox, the section headings still apply but the implementation specifics live in Modal as the reference.

## Step 1: Pick the right base

- `VpsDockerProvider` (`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py`) — VM with a public IP, Docker on top, SSH into the container. Override 4-6 hooks, inherit the rest.
- `ProviderInstanceInterface` — when the model isn't "VM + Docker" (Modal-shape).
- `BaseProviderInstance` — local providers only.

Hook contract: `provider-shape.md` §4.

## Step 2: Config class

Inherit `VpsDockerProviderConfig`. Add `default_region`, instance type / VM size, credentials, disk size, image override.

Defaults to share with the fleet: see `provider-shape.md` §3 (idle, auto-shutdown, disk size, region/zone, image, tags, SSH key location, exposure). Key points:

- `default_idle_timeout = 800` seconds.
- 30 GB default disk.
- `auto_shutdown_seconds`: default `None`; if set, MUST actually stop billing (the Azure trap — see Common Gotchas).
- `allowed_ssh_cidrs`: cloud-trio standard is `("0.0.0.0/0",)` with runtime warning. SSH is key-only.

Reference: `libs/mngr_aws/imbue/mngr_aws/config.py`.

## Step 3: Client class

Single class wrapping the cloud's API. Standard methods: `create_instance`, `destroy_instance`, `list_instances`, `get_instance_status`, `add_tags`, `remove_tags`, key-management calls if applicable.

Tag convention (`provider-shape.md` §3.8), dashes not underscores: `mngr-host-id`, `mngr-provider`, `mngr-created-at`, `mngr-pytest-launched`.

Error translation: cloud SDK exceptions become `VpsApiError` with an HTTP-style status. Downstream code branches on `status_code`.

## Step 4: Subclass `VpsDockerProvider`

Override roughly in this order:

- `_parse_build_args` (required). Compose `parse_vps_build_args(provider_prefix="--<yourname>-")` + the `extract_*` helpers from `libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py`. Reject unknown flags via `raise_if_unknown_provider_arg`; reject migration flags via `raise_if_vps_migration_arg`.
- `_fetch_provider_instances`. Return raw instance dicts filtered to `mngr-provider=<self.name>`. Called once per command via `_list_instances_cached`.
- `_list_provider_vps_hostnames`. Return SSH-reachable hostnames.
- `_create_vps_instance`. Override only for provider-specific knobs.
- `_validate_provider_args_for_create`. Cheap preflight before the first provider-side write. Pattern: `libs/mngr_gcp/imbue/mngr_gcp/backend.py` (firewall preflight + project-resolution warning + pytest cost-safety gate).
- `stop_host` / `start_host`. Override if `supports_shutdown_hosts=True` and you want to stop the VM. Pattern: `libs/mngr_aws/imbue/mngr_aws/backend.py`. Absence of these on Azure/GCP/Vultr/OVH is why those providers leak compute today.

Capability flags are honesty contracts: `True` means the method does the user-expected thing; `False` means it raises clearly. See `provider-shape.md` §2.

## Step 5: Operator commands

If your provider creates per-user backend resources (security group, firewall rule, IAM role), ship `mngr <yourname> prepare` and `mngr <yourname> cleanup`. Patterns: AWS, GCP, Azure `cli.py`.

- `prepare` is idempotent.
- `cleanup` refuses while user resources exist; tag-scoped (never touch infrastructure lacking a `mngr-*` tag).

Register via the `register_cli_commands` hookimpl in `backend.py`. If your provider has no per-user resources (Modal-shape, local), skip this — don't add a no-op for parity.

## Step 6: Cost safety

Cost leaks are the most expensive bug class.

- Pytest gate: `_validate_provider_args_for_create` raises when `PYTEST_CURRENT_TEST` is set and `auto_shutdown_seconds` isn't. Model: `libs/mngr_aws/imbue/mngr_aws/backend.py:200-225`.
- Orphan scanner: `pytest_sessionfinish` in `conftest.py` force-deletes `mngr-pytest-launched=true` resources older than a TTL. Model: `libs/mngr_aws/imbue/mngr_aws/conftest.py:134-180`. Vultr and OVH skipped this and leak real VPSes.
- `auto_shutdown_seconds`: must actually stop billing. Verify with a cloud-API probe — on Azure, `shutdown -P` leaves the VM "Stopped (not deallocated)" and the meter keeps running.
- Idle watcher: if `supports_shutdown_hosts=True`, ship one. AWS pattern: in-container watcher writes a sentinel; outer-host systemd `.path` unit fires `aws ec2 stop-instances`.

## Step 7: N agents per host

The interface is `(host_id, agent_id)`-keyed throughout. Your provider must allow `mngr exec <host> --new-agent` to add a second agent and must preserve all N across stop/start. Spec: `provider-shape.md` §1.8.

For the offline mirror (showing N agents while VM is stopped), AWS's per-field tag scheme is the model: `mngr-agent-<id>-name`, `-type`, `-labels`. If your cloud has a tag-count limit, surface it with a clear `NotImplementedError` (AWS caps at the EC2 50-tag wall, ~16 agents).

## Step 8: Tests

- **Unit tests:** config parsing, build-arg parsing (happy path + unknown-flag rejection + `--vps-*` migration rejection), capability-flag pinning, credentials-error classification (missing creds raise `ProviderUnavailableError`), cross-region refusal, networking warnings, `auto_shutdown_seconds` flowing through to the cloud API.
- **Release tests** (`test_release_<yourname>.py`, marked `@pytest.mark.release`): follow the trip structure in `specs/provider-release-tests.md`. Trip 1 = full lifecycle + sketchy-kill + gc; Trip 1b = second agent; Trip 2 = auto-shutdown; Trip 3 = snapshot survives destroy; Trip 4 = error classification.
- **Mock fidelity:** stub at your client class's surface, not at the cloud SDK. Pattern: `_FakeEc2Client` in `libs/mngr_aws/imbue/mngr_aws/testing.py`.

## Step 9: Documentation

`libs/mngr_<yourname>/README.md`: Setup (credentials), Build args, RBAC/IAM scopes for `prepare` / `create` / `cleanup`, Multi-region behavior, Defaults, Caveats (any divergence from the shape doc — be explicit).

Changelog: `libs/mngr_<yourname>/changelog/<branch-name>.md` (slashes replaced with dashes). CI fails without it.

## Step 10: Sanity-check against the shape doc

Walk `provider-shape.md` §10. Short form: capability flags honest; build-args reject unknown and `--vps-*`; every resource tagged with the three required keys; `ProviderUnavailableError` carries curated `user_help_text`; orphan scanner wired; `auto_shutdown_seconds` actually stops billing in a release test.

## Common gotchas

- `supports_volumes=True` on the VPS-Docker family is a lie — `list_volumes()` returns `[]`. Decide deliberately.
- SSH's `supports_shutdown_hosts=True` with `stop_host` raising `NotImplementedError` is a lie. Don't replicate; flip the flag.
- Modal uses underscore tag keys. Everyone else uses dashes. Use dashes.
- `auto_shutdown_seconds` semantics diverge: AWS terminates, GCP deletes, Azure halts-but-bills. Decide which yours is; verify with a cloud-API probe.
- Vultr/OVH have no managed firewall — they're public-by-construction. New cloud providers should ship a managed-firewall integration.
- `start_host(snapshot_id=…)` silent no-op on the VPS family. Honor it or raise `SnapshotsNotSupportedError`.
- `boto3.Session(region_name=self.default_region)` silently overrides `AWS_REGION`. Defer to env first.
- Disk-size field naming varies (`root_volume_size_gb` / `os_disk_size_gb` / `boot_disk_size_gb`). Use the cloud's own term.

## References

- `specs/provider-shape.md` — the contract.
- `specs/provider-uniformity-review.md` — observed reality across the nine in-tree providers.
- `specs/provider-release-tests.md` — release-test trip proposal.
- `libs/mngr_aws/imbue/mngr_aws/` — reference cloud-VPS provider.
- `libs/mngr_modal/imbue/mngr_modal/` — reference hosted-sandbox provider.
- `libs/mngr_vps_docker/` — shared base.
