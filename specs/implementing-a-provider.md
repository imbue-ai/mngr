# Implementing a new `mngr` provider

So you've been asked to add a new provider -- maybe DigitalOcean, Hetzner, Fly.io, or some internal cluster. Welcome. This guide walks you through the work in the order you'll encounter it. It's the dev-facing complement to `specs/provider-shape.md` (the prescriptive contract): where the shape doc says "you MUST do X," this doc says "here's where you'll do X, here's the gotcha that's bitten three people."

Read `specs/provider-shape.md` first. It's short and tells you what `mngr` users expect from every provider; this guide tells you how to deliver that on a new backend.

## Before you start

A "provider" is a pluggable backend that allocates compute, runs a Docker container (or sandbox) on it, talks to it via SSH, and tears it down. Users invoke it through `mngr create -p <yourname>`, `mngr list`, `mngr stop`, `mngr start`, `mngr destroy`, and `mngr <yourname> prepare` / `cleanup`. The single most important user expectation is that **`mngr` feels the same across providers**. Where that's impossible, be loud about the gap (raise, or flip a capability flag); never silently no-op.

Most likely your provider is one of:

- **Cloud VPS / VM** (DigitalOcean, Hetzner). Subclass `VpsDockerProvider`. Start from `libs/mngr_aws/imbue/mngr_aws/`.
- **Hosted sandbox** (Fly Machines, Modal-shaped). Implement `ProviderInstanceInterface` directly. Reference: `libs/mngr_modal/imbue/mngr_modal/`.
- **Local / BYO** (Lima, Docker, SSH). These already exist; you probably aren't writing one.

See `specs/provider-shape.md` §9 for the local-vs-cloud taxonomy.

## Step 1: Pick the right base

- **`VpsDockerProvider`** (`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py`) -- when your model is "VM with a public IP, Docker on top, SSH into the container." 5 of 9 in-tree providers do this; override 4-6 hooks, get the rest free.
- **`ProviderInstanceInterface`** directly -- when the model isn't "VM + Docker," like Modal's sandboxes.
- **`BaseProviderInstance`** -- for local providers. You almost certainly don't need this.

For the rest of this guide I'll assume cloud VPS. The hook methods you'll touch are roughly `_create_vps_instance`, `_fetch_provider_instances`, `_list_provider_vps_hostnames`, `_parse_build_args`, `_validate_provider_args_for_create`, and optionally `stop_host` / `start_host`. The shape doc §4 documents each contract.

## Step 2: Write your config class

Inherit `VpsDockerProviderConfig`. Add the usual: `default_region`, instance type / VM size, credentials, disk size, image override.

Defaults to share with the rest of the fleet (shape §3):

- `default_idle_timeout = 800` seconds
- Disk size: 30 GB
- VM image: pin a specific SKU; don't chase "latest"
- `auto_shutdown_seconds`: default `None`; when set, it MUST actually stop billing (see the Azure trap in Step 6)
- `allowed_ssh_cidrs`: the cloud-trio standard is `("0.0.0.0/0",)` with a runtime warning -- key-only SSH is the actual control. See shape §3.1.

Gotchas:

- **Env-var vs `default_region`**: `boto3.Session(region_name=self.default_region)` silently overrides `AWS_REGION`. Defer to env first.
- **Disk-size field naming** varies by cloud (`root_volume_size_gb` / `os_disk_size_gb` / `boot_disk_size_gb`). Use the cloud's own term -- don't standardize.

Reference: `libs/mngr_aws/imbue/mngr_aws/config.py`.

## Step 3: Write your client

A single class wrapping the cloud's API into a typed surface. Standard methods: `create_instance`, `destroy_instance`, `list_instances`, `get_instance_status`, `add_tags`, `remove_tags`, key-management calls if your cloud has them.

Tag convention (shape §3.8), **dashes, not underscores**:

- `mngr-host-id=<host_id>`
- `mngr-provider=<provider_instance_name>`
- `mngr-created-at=<ISO-8601>`
- `mngr-pytest-launched=true` on test-created resources

Modal uses underscores (`mngr_host_id`); don't copy that.

Per-host SSH keys: upload at create, delete at destroy. AWS uses `ImportKeyPair`; Azure injects via cloud-init in memory only. Either is fine.

Error translation: cloud-API exceptions become `VpsApiError` (in `libs/mngr_vps_docker/imbue/mngr_vps_docker/errors.py`) with an HTTP-style status. Don't let raw SDK exceptions leak past your client boundary -- downstream code branches on `VpsApiError.status_code`.

## Step 4: Subclass `VpsDockerProvider`

Override these, roughly in this order:

- **`_parse_build_args`** (`@abstractmethod`). Compose helpers from `libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py` (`extract_single_value_arg`, `extract_presence_flag`, `extract_git_depth`). Use `parse_vps_build_args(provider_prefix="--<yourname>-")`. Reject unknown flags via `raise_if_unknown_provider_arg`; reject `--vps-*` migration flags via `raise_if_vps_migration_arg`.
- **`_fetch_provider_instances`**. Return raw instance dicts filtered to `mngr-provider=<self.name>`. Called once per command via `_list_instances_cached`.
- **`_list_provider_vps_hostnames`**. Return SSH-reachable hostnames for those instances.
- **`_create_vps_instance`**. Override only if you need provider-specific knobs (AWS threads `ami_id_override`).
- **`_validate_provider_args_for_create`**. Cheap preflight before the first provider-side write. GCP's pattern at `libs/mngr_gcp/imbue/mngr_gcp/backend.py:101-149` is the model -- pre-flight the firewall, raise cleanly if a prerequisite is missing. You'll wire the pytest cost-safety gate here (Step 6).
- **`stop_host` / `start_host`** -- override these if `supports_shutdown_hosts=True` and you want to stop the VM. AWS at `libs/mngr_aws/imbue/mngr_aws/backend.py:335-446` is the model; the absence of these overrides on Azure/GCP/Vultr is why those providers leak compute today.

Be honest about capability flags. See shape §2: `True` means the method does what users expect; `False` means it raises clearly. `True` with a no-op implementation is the worst option.

## Step 5: Operator commands

If your provider creates per-user backend resources (security group, firewall rule, IAM role), ship a `mngr <yourname> prepare` / `cleanup` pair. Models: AWS, GCP, Azure `cli.py`.

- `prepare` MUST be idempotent.
- `cleanup` MUST refuse while user resources exist, with a "destroy them first" hint.
- `cleanup` MUST be tag-scoped. Never touch infrastructure lacking a `mngr-*` tag.

Register via the `register_cli_commands` hookimpl in `backend.py`:

```python
@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    return [my_provider_cli_group]
```

If your provider has no per-user resources (Modal, local providers), skip this. Don't add a no-op for parity.

## Step 6: Cost safety

Cost leaks are the most expensive bug class in this codebase, and the easiest to ship by accident:

1. **Pytest gate.** `_validate_provider_args_for_create` raises when `PYTEST_CURRENT_TEST` is set and `auto_shutdown_seconds` isn't. Model: `libs/mngr_aws/imbue/mngr_aws/backend.py:200-225`.
2. **`pytest_sessionfinish` orphan scanner** in `conftest.py`. Force-delete `mngr-pytest-launched=true` resources older than a TTL. Model: `libs/mngr_aws/imbue/mngr_aws/conftest.py:134-180`. Don't skip this -- Vultr and OVH skipped it and leak real VPSes.
3. **`auto_shutdown_seconds` actually terminates.** Cloud-init `shutdown -P +N` halts the OS. On AWS that triggers `InitiatedShutdownBehavior=terminate`. **On Azure it leaves the VM "Stopped (not deallocated)" and the meter keeps running.** Test that your value reaches the cloud API call -- not just the pre-create gate.
4. **Idle watcher.** If `supports_shutdown_hosts=True`, ship one. AWS's pattern (in-container watcher writes a sentinel; outer-host systemd `.path` unit fires `aws ec2 stop-instances`) is the model.

## Step 7: N agents per host

The interface is `(host_id, agent_id)`-keyed from the start. Your provider MUST allow `mngr exec <host> --new-agent` to add a second agent and MUST preserve all N across stop/start. See shape §1.8.

For the offline mirror (showing N agents while the VM is stopped), AWS's per-field tag scheme is the model: `mngr-agent-<id>-name`, `-type`, `-labels`. See `libs/mngr_aws/imbue/mngr_aws/backend.py:66-87`. AWS caps at EC2's 50-tag limit (~16 agents) and raises `NotImplementedError` with a clear message. If your cloud has a similar limit, surface it the same way -- don't silently drop data.

## Step 8: Tests

**Unit tests:** config parsing, build-arg parsing (happy path + unknown-flag rejection + `--vps-*` migration rejection), capability-flag pinning, credentials-error classification (missing creds raise `ProviderUnavailableError`, not `ProviderEmptyError`), cross-region refusal, networking warnings on wide CIDRs, and -- if you can -- `auto_shutdown_seconds` flowing through to the cloud API (currently a hole everywhere).

**Release tests** (`test_release_<yourname>.py`, marked `@pytest.mark.release`): follow the trip structure in `specs/provider-release-tests.md`. Trip 1 = full lifecycle + sketchy-kill + gc; Trip 1b = second agent on the same host; Trip 2 = auto-shutdown actually stops billing; Trip 3 = snapshot survives destroy (if `supports_snapshots`); Trip 4 = error classification. One boot amortized across many assertions, ~5-15 min wall-clock each.

**Mock fidelity:** stub at the client layer, not at boto3 / azure-sdk. AWS's `_FakeEc2Client` in `libs/mngr_aws/imbue/mngr_aws/testing.py` is the model -- a small in-memory dict implementing the subset of `Ec2Client` your provider actually calls.

## Step 9: Documentation

`libs/mngr_<yourname>/README.md` should cover Setup (credentials), Build args, RBAC/IAM scopes for `prepare` / `create` / `cleanup`, Multi-region behavior, Defaults, and Caveats (anywhere you diverge from the shape doc -- be loud). Don't be Modal: 42 lines with no Setup section is the cautionary tale.

Plus a changelog entry at `libs/mngr_<yourname>/changelog/<branch-name>.md` (replace `/` with `-`). CI fails without it.

## Step 10: Sanity-check against the shape doc

Walk the implementer checklist in `specs/provider-shape.md` §10. Short version: `supports_*` flags are honest; build-args reject unknown and `--vps-*`; every resource tagged with the three required keys (dashes); `ProviderUnavailableError` carries curated `user_help_text` (don't fall through to "start Docker" -- see `_azure_unavailable_error`); orphan scanner is wired; `auto_shutdown_seconds` actually stops billing in a release test.

## Common gotchas you'll hit

- **`supports_volumes=True` is a lie on the VPS-Docker family** -- `list_volumes()` returns `[]`. Decide deliberately.
- **SSH provider's `supports_shutdown_hosts=True` lie** -- it raises `NotImplementedError`. Don't replicate; flip the flag.
- **Modal uses underscore tag keys** -- everyone else uses dashes. Use dashes.
- **`auto_shutdown_seconds` semantics diverge:** AWS terminates, GCP deletes, Azure stops-but-bills. Decide which yours is; verify with a cloud-API probe.
- **Vultr/OVH have no managed firewall** -- they're public-by-construction. New cloud providers should ship a managed-firewall integration.
- **`start_host(snapshot_id=…)` silent no-op on the VPS family.** Honor it or raise `SnapshotsNotSupportedError`. Silent ignore is the worst.

## Where to go for help

- **`specs/provider-shape.md`** -- the contract.
- **`specs/provider-uniformity-review.md`** -- observed reality across the 9 in-tree providers; lifecycle matrices, defaults table, top findings, and recommendations.
- **`specs/provider-release-tests.md`** -- the release-test trip proposal.
- **`libs/mngr_aws/imbue/mngr_aws/`** -- reference cloud-VPS provider.
- **`libs/mngr_modal/imbue/mngr_modal/`** -- reference hosted-sandbox provider.
- **`libs/mngr_vps_docker/`** -- the shared base.

You're not the first person to do this and the worn paths show. Lean on AWS for cloud-VPS shape, Modal for hosted-sandbox shape, the shape doc when you're unsure whether a divergence is OK. Adding a new provider is a few hundred lines of mostly glue; most of the work is being honest about the cost story and the capability flags. Get those right and the rest follows.
