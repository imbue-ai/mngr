Integration branch for the imbue_cloud slice-fleet generation 2 program (raw qemu slices with routed-tap networking on Debian 13 boxes, replacing lima), merged together with `main` and the three pre-cutover fix PRs: imbue-ai/mngr-internal#857 (SSH certificates from Vault, #850), #855 (DHCP placement, #849) and #856 (the upstream artifact mirror, #851). The per-change details live in this directory's constituent entries, listed below.

Deploy docs for the gen-2 fleet (cutover runbook, management plane, telemetry, host-pool setup, the next-deploy checklist), the per-tier `deploy.toml` additions (`[ssh_ca]`, the `ssh-ca` service, management-plane config), and the read-only machine-size display in the desktop client.

Constituent entries: `new-fleet-base.md`, `new-fleet-runsc-prototype.md`, `new-fleet-phase-2.md`, `new-fleet-phase-4-impl.md`, `new-fleet-phase-5.5.md`, `mngr-variable-sizing.md`, `mngr-machine-size-display.md`, `mngr-slice-fleet-gen2-phase-3.md`, `mngr-slice-fleet-gen2-phase-4.md`, `mngr-new-fleet-testing.md`, `mngr-slice-fleet-canary-followups.md`, `mngr-finish-new-fleet-canary-testing.md`, `mngr-identify-new-fleet-issues.md`, `mngr-onetun-install-and-canary.md`, `mngr-lima-rename.md`, `mngr-ssh-authority-in-vault.md`, `mngr-remove-init-via-dhcp.md`, `mngr-mirror-upstream-artifacts.md`

Gen-2 small follow-ups: the deploy docs, the `[ssh_ca]` field descriptions, and the commented `[ssh_ca]` blocks in every tier's `deploy.toml` now name the tier's Vault SSH CA mount `minds-<tier>-ssh` (was `ssh-<tier>`; imbue-ai/vault#11 after review), e.g. `vault read -field=public_key minds-dev-ssh/config/ca`; the `vault_reader` SSH signing test fixtures use the new name too.

Gen-2 small follow-ups: the per-tier `management_plane.toml` is gone; a tier's gen-2 management plane (operator WireGuard peers and the Modal Proxy whose static IPs the box `:22` lockdown allowlists) is now the optional `[management_plane]` table of its `deploy.toml` (`[management_plane.wireguard]`, `[[management_plane.wireguard.operators]]`, `[management_plane.modal_proxy]`; `DeployEnvConfig.management_plane`, None when absent, exactly like `[ssh_ca]`). The dev tier's table moved over verbatim; ci, staging, and production carry a commented pointer. `load_management_plane_config_or_none` is removed: read `load_deploy_config(tier).management_plane`, and the operator-address block check now runs inside `load_deploy_config`.

Gen-2 small follow-ups: the `[management_plane]` validation errors (duplicate operator names, addresses, or public keys; an out-of-range `listen_port`; a named proxy with no `static_ips`) name the nested `[management_plane.wireguard]` / `[management_plane.modal_proxy]` tables they refer to.

Dev and ci tier bring-up (2026-09-09): `envs/dev/deploy.toml` and `envs/ci/deploy.toml` now carry the `[ssh_ca]` public keys of the `minds-dev-ssh` and `minds-ci-ssh` Vault mounts created by imbue-ai/vault#11, so gen-2 box prep and slice bakes on those tiers no longer refuse; the no-CA pin test now covers staging and production only, and a new test pins each committed key.

Fixed (found on the dev-tier test pass): the deployment tests that lease a pool host directly (`test_pool_lease`, `test_workspace_stop_start`, `test_machine_resize`, `test_quota_enforcement`) now send `max_box_generation` (the shared `LEASE_MAX_BOX_GENERATION` in `deployment_tests/helpers.py`); without it the connector treats the caller as a pre-gen-2 client and confines the lease to gen-1 rows, so against a gen-2-only pool every one of them reported "no capacity" and skipped.

Docs (found during the gen-2 final test pass, 2026-09-10): the box-ordering runbook and the `server-order` recipe comment name the production box plan as `24sys03-v1-us` (Xeon-E 2288G, 128 GB, `softraid-2x960nvme`, 14 slices); OVH's eco catalog no longer lists the `24sys032-us` code they used to name, so a copy-pasted order failed with "plan not found". The option families (`bandwidth-1000-24sys-us`, `vrack-bandwidth-500-24sys-us`) and the $160 first-month price are unchanged.

`next_deploy.md`: recorded that the CI infra DB and the dev envs have applied the gen-2 connector migrations (028-041 on CI infra, 034-041 via `env deploy` on dev-josh-2 / dev-gen2mig).

`gen2-cutover.md`: the rollback section now mentions that a mid-migration rollback re-stamps the harvested host keys onto the row.

`gen2-cutover.md`: says that a running cutover stage can be killed with Ctrl-C or SIGTERM and re-run to resume, and corrects the `--keep-origin-vm` note: the kept origin VM is never collected by the orphan reap (it still carries the migrated row's instance name) and must be destroyed by hand.

Docs: the glossary's "adoption" entry and the lost-device runbook's `hosts rotate` description now say that the client remembers the endpoints it last pinned an adopted workspace's host keys at and moves the pins to the connector's current endpoints before every connection, so the workspace stays reachable after a restore driven by an operator, a rollback, or another device, and that a rotation's synced pins retire the previous host keys on every device; the behavior itself is in the `mngr_imbue_cloud` entry.

`test_create_workspace_and_sign_in_via_modal_then_chat_via_electron` (the snapshot-resume Electron sign-in test) is marked flaky: on 2026-09-10 it failed one PR run with a playwright `Frame.click` timeout and passed on re-run, with no change to the flow under test.


Fixed (found on the 2026-09-12 desktop test pass): a second device signed in to the same account could see a freshly created cloud machine before it could open it. The create path seeded the synced record with no provider and no secrets, and the SSH key only followed with the reconcile's refresh up to a sync tick later; a device that pulled in that window read the account as not locked (so no unlock banner) and offered the machine as openable, which then hung on a blank surface until the forward gave up. Three changes: the create-path seed now carries the cloud provider and, when the account is unlocked, the SSH key and pins the lease already wrote (the first push is complete); the reconcile mirrors the account's key bundle from the connector on a device that holds none, so the account reads as locked from its first sync and the unlock needs no further round trip; and a live cloud row this device holds no key for is not clickable and carries a chip saying why (`Enter your master password to open`, `Syncing access…`, or `No access from this device`), with its open-in-new-window button withheld. The row's new `key_state` field rides the `workspaces` channel message.

Docs: the environments reference's desktop-client and destroy sections and the testing overview now state that one machine must never run two minds instances against the same env at once (see the `minds-admin env stop-local` entry in `apps/minds_admin`), and that the destroy preflight recognizes a dev launch.


`gen2-telemetry.md`: the prep-artifact integrity list now includes the S3 IPv4 pin refresher script and its service and timer.

`next_deploy.md`: the provisional `minds-v0.6.0` tags were deleted rather than re-pointed; the real tag is cut from `main`.
