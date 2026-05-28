# Unabridged Changelog - mngr_ovh

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_ovh/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
