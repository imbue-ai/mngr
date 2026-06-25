# Unabridged Changelog - mngr_ovh

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_ovh/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-22

An unauthenticated OVH provider now errors instead of silently reporting zero agents.

Previously, when no OVH credentials were resolvable anywhere (config, `OVH_*` env vars, `~/.ovh.conf`), the provider silently returned an empty listing (exit 0). It now raises the shared `ProviderNotAuthorizedError` at construction, so an enabled-but-unauthenticated OVH provider is reported consistently with the other cloud providers (one consistent error line in `mngr list`, contributing a non-zero exit) rather than vanishing.

## 2026-06-19

Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename: the VPS
provider is no longer Docker-only, so the package and its shape-agnostic base
classes dropped "Docker" from their names (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerHostStore` -> `VpsHostStore`, `VpsDockerError` ->
`VpsError`). Import-only change; no behavior difference.

Updated the VPS build-arg parsing imports to point at the new `imbue.mngr_vps.build_args` module (moved out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

`mngr ovh list` now resolves its `[providers.<name>]` block via the shared `mngr_vps.cli_helpers.resolve_provider_config` instead of an OVH-local copy. No behavior change; the wrong-backend warning still fires when `--provider` points at a non-OVH block.

Trimmed the README to user-relevant content (removed internal implementation details and deep provisioning mechanics) and tightened it for concision.

Aligned the OVH provider config field descriptions (surfaced via `mngr config`/help) with the README's configuration table, and corrected a stale default-plan price.

Documented the `~/.ovh.conf` fallback for OVH credentials in the field descriptions.

## 2026-06-16

## OVH provider

- `mngr ovh list` now reads its defaults from the user's `[providers.<name>]` settings.toml block (selected with `--provider`, default `ovh`), matching `mngr aws prepare` / `mngr gcp prepare` / `mngr azure prepare`. Previously it built `OvhProviderConfig()` with class defaults unconditionally, so it always talked to the default endpoint / subsidiary (`ovh-us` / `US`) regardless of what the user pinned -- a user who configured a non-default `endpoint` / `ovh_subsidiary` in their provider block (e.g. `ovh-eu`) and ran `mngr ovh list` would inspect a different account than the runtime `mngr create --provider <name>` path uses. Credentials still fall back to env / `~/.ovh.conf` when the block leaves them unset. A warning is logged if the named `--provider` block exists but is not an OVH backend.

- `mngr ovh list` groups its OVH-specific options (`--provider`, `--all`) under a "Provider" option group, so `--help` and the generated docs list them ahead of the shared common options instead of below them.

- The OVH release-test settings now also disable the `azure` provider (`[providers.azure] is_enabled = false`), mirroring the existing gcp/aws/vultr disables. Without it, `mngr list` inside the OVH lifecycle tests would enumerate the newly-added azure provider and exit non-zero when Azure credentials weren't resolvable in that subprocess, failing the OVH tests for a non-OVH reason.

Removed the dead VPS client methods `create_snapshot`, `delete_snapshot`, `list_snapshots`, and `list_ssh_keys` (and the now-unused `_safe_get_snapshot` and `_snapshot_info_from_payload` helpers) from `OvhVpsClient`. These had no production callers and are being dropped from the shared `VpsClientInterface`. The corresponding unit and release tests were removed as well.

## 2026-06-15

## Internal: `_provision_vps` signature follows the shared base

- `OvhProvider._provision_vps` now accepts the `vps_public_key` parameter that the shared `VpsDockerProvider.create_host` threads in (so it no longer re-reads the provider SSH keypair from disk inside the base implementation). OVH installs the SSH public key via its rebuild API rather than the base cloud-init path, so the parameter is accepted and ignored. No behavioral change for OVH.

- The OVH release tests write a `settings.toml` that disables every other remote provider so the create-host preflight does not trip resolving their credentials. With the new `gcp` provider now registered as a remote backend, it is added to that disable-set. No behavioral change for OVH.

## 2026-06-12

## AWS provider support: shared VPS-Docker base changes

- `is_for_host_creation` flag removed; replaced with the default-no-op `bootstrap_for_host_creation` hook on `ProviderBackendInterface`. The OVH backend's `del`-of-`is_for_host_creation` is removed; no behavior change.
- `get_build_args_help()` no longer carries the stale "OS image is set via default_image_name..." block — that line described the removed `--vps-os=` shared build arg, not current OVH behavior.
- `OvhVpsClient` picks up the shared `wait_for_instance_active` interface change (now a default method on `VpsClientInterface`).
- **Per-host build args renamed**: `--vps-datacenter=` is now `--ovh-datacenter=` (`--ovh-region=` is accepted as an alias). `--vps-plan=` is now `--ovh-plan=`. The old `--vps-*` prefix raises a migration error. `--git-depth=` stays shared.
- `vps_boot_timeout` config field renamed to `instance_boot_timeout` (matches the base-config rename).
- **OVH release-test fix**: the two `TestOvhProviderLifecycle` `mngr create` invocations now pass `--type claude`, matching the Vultr and AWS release tests. Previously they relied on a configured default agent type, which is never present in the isolated test HOME, so the lifecycle tests failed immediately with "No agent type provided" and could not exercise a real OVH VPS create/exec/destroy cycle.

## 2026-06-10

Raised the stale coverage floor from 60% to 75% to match the coverage CI already measures (~79%).

## 2026-06-08

OVH provisioning now applies the shared `mngr_vps_docker` host-setup steps over
SSH (OVH has no cloud-init). This closes a real gap: OVH never installed gVisor
`runsc` before, so `[providers.ovh] install_gvisor_runtime = true` was silently a
no-op and OVH-baked hosts (including the imbue_cloud pool) ran the agent
container under the default runtime. With this change OVH installs the pinned
Docker version, registers `runsc` when `install_gvisor_runtime` is set, tunes
sshd, installs the required outer packages, and purges qemu -- all from the
single shared source of truth.

The OVH-specific `install_required_outer_packages` and `purge_qemu_packages`
bootstrap helpers are removed; their behavior is now folded into the shared
host-setup step list as config-gated steps.

Tests now isolate $HOME the same way as every other mngr plugin: the project
conftest calls `register_plugin_test_fixtures(globals())`, which brings in the
autouse `setup_test_mngr_env` fixture. Previously this plugin's tests did not
redirect $HOME, so running them on their own could read or write the real
`~/.mngr` / `~/.claude.json`. Internal test-infrastructure change only; no
user-facing behavior change.

- Now auto-discovered as a publishable package by the release tooling (it is a standalone `mngr ovh` VPS-provider plugin, a peer of the already-published `mngr_vultr`). It will be offered for first publication to PyPI on the next release. Its internal pins were already current; no runtime change.

## 2026-06-05

- Added to the release tooling's publish graph (`scripts/utils.py`). It will be offered for first publication to PyPI on the next release. No runtime change.

## 2026-06-04

Recycled OVH VPSes now receive the new bake's extra IAM tags (e.g. `minds_env=<env>`), overwriting any stale value left by the previous owner. Previously the recycle path only swapped `mngr-host-id` and skipped extra tags entirely, so a pool host provisioned by recycling a cancelled VPS carried no `minds_env` tag (or a stale one). That made it invisible to env-scoped discovery/teardown (`minds env destroy`, which enumerates VPSes by the `minds_env` IAM tag) and obscured which env actually owns a recycled host. Fresh-order behavior is unchanged.

Fresh-order pool bakes no longer fail intermittently with "Action not available while there are running tasks on the VPS". OVH's task listing is eventually consistent, so the pre-`/rebuild` drain could report no active tasks while OVH still rejected the rebuild because the post-delivery `deliverVm` task was in flight. The rebuild POST is now retried (re-draining each round, up to 5 minutes) until OVH accepts it, treating OVH's own rejection -- not the laggy task listing -- as the authoritative "task still running" signal. Non-task-related rebuild errors still surface immediately.

The `PUT /vps/{s}/serviceInfos` cancel/un-cancel call (`set_renew_at_expiration`) now also retries transient transport failures (dropped connection / timeout), not just the "subscription is not active yet" billing-propagation case. This hardens the failure-cleanup cancel path, where a single dropped connection previously leaked a freshly-ordered month of billing. Non-transient API errors (other 400s/404s/5xx) still surface immediately.

OVH-provisioned hosts now have OVH automated backups disabled. As the final bootstrap step, the OVH provider purges all `qemu*` packages (`apt-get purge --auto-remove 'qemu*'`) over SSH on each freshly-ordered or recycled VPS. OVH backups drive the image's `qemu-guest-agent` to freeze the guest filesystem, which caused serious runtime problems on the agent; removing qemu removes the mechanism the backups hook into. The purge runs on both the fresh-order and recycle paths (rebuilding the OS reinstalls the agent), and a failure aborts provisioning so no host is left running with backups enabled. mngr also never orders an OVH backup option in the order/cart flow (now covered by a regression test). Existing already-running OVH hosts are not swept; they pick up the purge when next recycled.

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-03

Discovery no longer masks failures as "zero hosts". `_list_provider_vps_hostnames`
previously caught any IAM-listing error and returned an empty list, so a
transient OVH outage / expired credentials looked identical to a real empty
result -- which the discovery layer can't distinguish, and which defeats mngr's
"mark hosts UNKNOWN when a provider's discovery fails" safeguard. It now lets the
error propagate (the genuinely-unconfigured case is still the early-return), so
`mngr list --on-error continue` records the failure instead of silently dropping
live hosts.

## 2026-06-02

Collapsed redundant `except` clauses: clauses listing `VpsApiError` / `VpsProvisioningError`
alongside `MngrError` now catch just `MngrError` (those VPS errors are already `MngrError`
subclasses via `VpsDockerError`). No behavior change.

- pyproject.toml: align `imbue-mngr*==` pin stragglers with the satellites bumped in main's `e22e7010e` release commit. Several `imbue-mngr-*` libs still pinned to older versions even though `libs/mngr` had moved to 0.2.10; building the apps/minds ToDesktop bundle from main today would fail at `uv lock` in `apps/minds/scripts/build.js` because the workspace constraint graph is unsatisfiable. Day-to-day dev hides this because `[tool.uv.sources]` redirects every `imbue-mngr-*` to its workspace path, bypassing the `==` pin.

## 2026-05-29

User-visible: minds workspaces running on OVH (docker-on-VPS) hosts can now
be backed up off-site (restic) when a backup provider is selected at creation
time.

(No code change in this project in this PR; the integration lives in the
minds app and the forever-claude-template `host_backup` service.)

Added `inotify-tools` and `jq` to `_REQUIRED_OUTER_PACKAGES` so the new
`snapshot_helper.service` provisioned by `mngr_vps_docker` has the tools
it needs on OVH-leased outers (the cloud-init path on Vultr / generic
VPSes pulls these in via the cloud-init `packages:` list).

OVH hosts created by `mngr create --provider ovh` now back their per-host
unified docker volume with a btrfs subvolume on a loop-mounted btrfs filesystem
on the VPS (`/mngr-btrfs/<host_id_hex>` on `/var/lib/mngr-btrfs.img`). This
makes future consistent snapshotting of the agent data via
`btrfs subvolume snapshot -r` possible. The setup happens in the shared
`VpsDockerProvider._setup_container_on_vps` path, so OVH's bootstrap (rebuild +
TOFU + root SSH + `rsync` install) is unchanged; the `apt-get install btrfs-progs`
runs on the freshly-bootstrapped root SSH session. See `mngr_vps_docker`'s
changelog for the full mechanism.

**Breaking change:** existing ovh hosts created before this release cannot
be discovered or managed after upgrade. Destroy and recreate them.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

End-to-end fixes for the OVH-backed imbue_cloud pool flow that surfaced
while smoke-testing the bake / lease / first-start sequence against a fresh
dev env.

### OVH outer-bootstrap installs `rsync`

The OVH `Debian 12 - Docker` image ships docker but not `rsync`, which the `mngr_vps_docker` build-context upload needs. Cloud-init-using backends (Vultr) inherit rsync from their base images; OVH has no cloud-init at all, so the gap surfaced as `bash: line 1: rsync: command not found` after every other outer-bootstrap step had already succeeded. New `install_required_outer_packages` helper in `mngr_ovh.bootstrap` runs as the final outer step before `VpsDockerProvider.create_host` takes over.

OVH provider: two correctness fixes to `OvhProvider._provision_vps` + `ordering.order_and_wait_for_vps` discovered while auditing PR #1671 (full audit in `OVH_AUDIT.md`).

- **F1**: `parse_extra_tags_env(os.environ.get("MNGR_VPS_EXTRA_TAGS", ""))` now runs at the very top of `_provision_vps`, before `_maybe_claim_recycled_vps` and before any OVH API call. Previously the parse ran AFTER `order_and_wait_for_vps`, so a typo in `MNGR_VPS_EXTRA_TAGS` (uppercase key, reserved key, missing `=`) raised only after we'd already ordered + paid for a fresh-month VPS. The spec explicitly required pre-order validation. Pinned by a source-position test in `backend_test.py` so a future refactor that moves the parse back down breaks the test loudly.
- **F39**: `OvhVpsClient.set_renew_at_expiration` now retries on the OVH transient 400 message `"Unable to synchronize l1::Service, subscription is not active yet"`. OVH's billing subsystem takes a few minutes to fully activate a freshly-ordered VPS subscription, during which any `PUT /vps/{name}/serviceInfos` (the cancellation flag flip) fails with this exact message; without the retry, `OvhProvider._terminate_orphaned_fresh_order`'s cleanup (fired from the `_provision_vps` `finally` branch when the fresh-order path raises after `order_and_wait_for_vps` succeeded) loses the race and silently leaks a freshly-ordered month of billing. Other 400s / 404s / 5xxs propagate immediately so unrelated client errors don't get swallowed. Retry uses `poll_for_value` with a 5-minute default budget + 15s poll interval (both injectable via new `set_renew_retry_timeout_seconds` and `set_renew_retry_poll_interval_seconds` fields on the client). Verified live on 2026-05-18: a `set_renew_at_expiration` call issued immediately after a fresh order failed once with this exact message; a 30-second retry succeeded. Three new tests in `client_test.py` cover the happy retry path, the "different 400 propagates immediately" guard, and the budget-exhausted error path.

- **F3**: `order_and_wait_for_vps` no longer diffs `/vps` listings to find the new serviceName. It captures the `orderId` from the checkout response and then walks the `/me/order/{orderId}/details/{detailId}/{extension,operations,operations/{opId}}` chain, matching on `extension.order.plan.code == requested_plan_code` to disambiguate the VPS line item from the OS / backup / installation sub-items, and reading the assigned serviceName from `service.Operation.resource.name`. **Strong correlation: every poll is scoped to OUR `orderId`, so two concurrent orders against the same OVH account can never swap serviceNames** -- the legacy diff approach picked `sorted(new_names)[0]` and would silently return the wrong VPS to one of the callers when two deliveries finished within the same poll interval. The OVH API's `billing.OrderDetail.domain` field, which an earlier version of this fix tried to use, is always the literal `"*"` for VPS orders (verified live against OVH-US on 2026-05-18); only the operations chain yields the assigned serviceName. Belt-and-suspenders: after fetching the serviceName, the function `GET /vps/{serviceName}` and verifies `model.name == requested_plan` and the requested datacenter is a case-insensitive substring of `zone`. On mismatch the function raises and the existing cleanup cancels future renewal on the wrong VPS. **End-to-end live-verified against the real OVH-US API**: one live `vps-2025-model1` order in US-EAST-VA returned `vps-c4aeb97e.vps.ovh.us` in ~80s; an independent script-side walk of the same operations chain (detail 105339987 -> operation 173487777 -> resource.name) returned the same name; the post-hoc verify saw the expected `model.name` + `zone`; the diff-against-`/vps` cross-check confirmed exactly one new VPS appeared. Unit tests in `ordering_test.py` cover the happy path, delayed detail-listing materialisation, the plan-code filter rejecting OS sub-resource details, post-hoc plan/region verify mismatch detection, missing-orderId refusal, delivery timeout, and a multi-thread parallel-orders regression test that runs two concurrent orders against a single shared fake client + asserts each thread returns its own serviceName (the legacy code would have failed this test).

Add the `mngr_ovh` provider plugin: run mngr agents in Docker containers on OVH classic VPS instances (e.g. `vps-2025-model1` / "VPS-1" at ~$7.60/mo).

- Uses the official `python-ovh` SDK; supports OAuth2, AK/AS/CK, and `~/.ovh.conf` credentials.
- Provisions via the OVH `/order/cart` flow and bootstraps via `POST /vps/{s}/rebuild` with a pre-installed SSH public key (no cloud-init is available on OVH classic VPS).
- Discovers VPSes via OVH IAM v2 tags (`POST /v2/iam/resource/{urn}/tag`) on the `vps` resource URN, so multiple `mngr` instances on different machines see the same agents.
- First SSH connection performs a TOFU pin of the host key into a per-provider `known_hosts` file; strict host-key checking is enforced from then on. See `libs/mngr_ovh/README.md` for the security caveat.
- `mngr create --provider ovh` automatically reuses a cancelled-but-still-alive OVH VPS (the leftover from a prior `mngr destroy` that OVH won't actually decommission until end of month) instead of ordering a fresh one. Controlled by `enable_recycle_cancelled` (default `True`), `recycle_safety_margin_hours` (default `24`), and `recycle_max_candidates_considered` (default `10`).
- Adds `mngr ovh list [--all]` operator command: shows every mngr-tagged OVH VPS in the account (or every VPS with `--all`) with plan, datacenter, state, expiration, cancellation status, and IAM tags (`mngr-provider`, `mngr-host-id`, `mngr-recycling-by`). Plain text table; one IAM-resource call plus parallel per-VPS detail fetches via `ConcurrencyGroupExecutor`.

- `mngr_ovh.OvhProvider` now honors `MNGR_VPS_EXTRA_TAGS=k1=v1,k2=v2` and attaches each entry as an OVH IAM v2 tag alongside `mngr-provider` / `mngr-host-id`. Parsing is strict with local IAM-key validation so typos fail before the API call.
- `OvhProviderConfig.recycle_safety_margin_hours` default drops 24 -> 2 so same-day destroy + create reclaims the cancelled VPS instead of ordering a fresh month.
- `mngr_ovh` README plan-size info is updated: `vps-2025-model1` is 1 vCPU / 8 GB RAM / 80 GB SSD at ~$7.99/mo (the previous README claim of 2 GB / $7.60 was stale).

Fixed three blocker bugs in the OVH provider that surfaced the first time `mngr create --provider ovh` was exercised end-to-end against a live OVH account.

- Post-delivery race: `order_and_wait_for_vps` no longer returns until the background `deliverVm` task drains, so the immediately-following `/rebuild` no longer fails with "Action not available while there are running tasks on the VPS". `rebuild_vps_with_public_key` also performs the same drain as a pre-flight so the recycle path is covered.
- `destroy_instance` now actually cancels the VPS via `PUT /serviceInfos` (`renew.deleteAtExpiration=true`) instead of `POST /terminate`. The legacy `/terminate` call only emails a confirmation token, so without a human acting on the email the VPS would auto-renew indefinitely.
- `set_renew_at_expiration(False)` now also restores `renew.automatic=true` and `renewalType=automaticV2012`, which OVH silently auto-flips when `deleteAtExpiration` goes to `true`. Without this, a recycled VPS would not auto-renew at the next anniversary even though the un-cancel flag flip succeeded.
- OVH's `Debian 12 - Docker` image installs the rebuild SSH key into `/home/debian/.ssh/authorized_keys` rather than `/root/.ssh/authorized_keys`. The provider now sudo-copies the key into root's home during provisioning (configurable via the new `bootstrap_ssh_user` field on `OvhProviderConfig`, defaulting to `debian`), so the rest of the provider continues to run as root without per-call sudos.
- The OVH `mngr-provider` / `mngr-host-id` IAM tags are now attached immediately after the VPS appears in `GET /vps`, before rebuild + TOFU + root-bootstrap. Any failure during those later steps now leaves an orphan VPS that is discoverable via mngr's normal IAM-tag listing instead of being invisible until inspected via `mngr ovh list --all`.
- The SSH-as-bootstrap-user / SSH-as-root paramiko sessions in the OVH provider now load the private key with a type-agnostic helper that tries Ed25519, RSA, and ECDSA in turn. Previously the call was hardcoded to `paramiko.Ed25519Key.from_private_key_file`, which raised against the RSA keys the base `VpsDockerProvider` actually produces; this had been masked until the Bug 1 fix let the provisioning flow reach the TOFU step.
- `OuterHost.get_name` and `OuterHostInterface.get_name` now return `str` instead of `HostName`. The outer host's name is the connector's literal connection target -- an SSH hostname or IP address -- which routinely contains dots (`vps-x.vps.ovh.us`, `192.0.2.10`) and was rejected by `HostName`'s validator (dots are reserved as the deterministic separator in CLI `HOST.PROVIDER` addresses). The `Host` subclass's `get_name` still returns `HostName`, which is a `str` subtype and so satisfies the wider interface.
