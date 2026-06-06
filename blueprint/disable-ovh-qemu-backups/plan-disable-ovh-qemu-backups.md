# Disable OVH-side VPS backups by purging qemu

## Refined prompt

we need to disable backups on ovh. This can be done by simply uninstalling qemu there.

To detect it, you can run:
    dpkg -l | grep qemu
To uninstall, you probably want to run something like:
    sudo apt-get purge --auto-remove 'qemu*'

This should be done at the ovh provider level--the backups cause serious runtime problems.

Make sure that we're not accidentally ordering / enabling backups when we create new ovh hosts anyway.

* Root cause: OVH automated backups rely on the image's `qemu-guest-agent`, whose filesystem-freeze hangs the VPS.
* Add a new helper in `mngr_ovh/bootstrap.py`, called from `OvhProvider._provision_vps`, that runs `apt-get purge --auto-remove 'qemu*'`, detecting first with `dpkg -l | grep qemu` to avoid an apt glob error when nothing matches.
* Use the broad `'qemu*'` pattern (matching the original instruction), not just `qemu-guest-agent`.
* Trust apt's exit code -- no separate verify-after step.
* The purge runs on both the fresh-order and recycle provisioning paths (both rebuild the OVH OS, which reinstalls qemu); scoped to new provisions only -- existing live hosts are not swept (all hosts will be recycled separately after this ships).
* Scope is `OvhProvider._provision_vps` (OVH OS bake + recycle-rebuild) only, NOT the imbue_cloud container-rebuild slow path; pool hosts are purged at OVH bake time and container teardown does not reinstall OS-level qemu.
* Purge failure is fatal: raise `VpsProvisioningError` so the normal create-cleanup tears the VPS down (consistent with sibling bootstrap steps; guarantees no host ever runs with backups enabled).
* Reuse the existing 300s apt timeout constant for the purge command.
* Confirm we never order OVH automated backups: the cart flow only configures `vps_datacenter`, `vps_os`, and `vps_install_rtm=no`; add a unit test with a recording `OvhVpsClient` that captures all `/configuration` calls and asserts none are backup-related.
* Add a short note to the OVH README documenting that qemu is purged so OVH backups stay off.
* Changelog entry for `mngr_ovh` only.
* The minds restic `BackupProvider` feature is explicitly out of scope and left untouched.

## Overview

- OVH classic-VPS images ship `qemu-guest-agent`, which lets OVH run automated backups that freeze the guest filesystem and cause serious runtime problems.
- The fix removes qemu from every OVH host mngr provisions, at the OVH provider level, so the backup mechanism has nothing to hook into.
- A new bootstrap helper purges qemu over SSH as the final post-rebuild step in `OvhProvider._provision_vps`, covering both fresh orders and recycled VPSes.
- We separately lock in that mngr never *orders* OVH automated backups: the order/cart flow already configures only datacenter / OS / RTM, and a regression test will keep it that way.
- Scope is intentionally narrow: new provisions only, OVH provider only; the minds restic backup feature is untouched.

## Expected behavior

- Every OVH VPS that mngr freshly orders or recycles has all `qemu*` packages purged before the host is handed off as ready.
- OVH-side automated/snapshot backups can no longer run on mngr-provisioned OVH hosts, eliminating the freeze-induced runtime problems.
- If the purge fails (apt error, SSH failure, timeout), provisioning fails with `VpsProvisioningError` and the freshly-ordered or recycled VPS is torn down by the existing create-cleanup path -- no host is ever left running with qemu present.
- On an image that somehow ships no qemu packages, the detect-first guard makes the step a clean no-op (no apt glob error), and provisioning proceeds normally.
- imbue_cloud pool hosts inherit the behavior: they are baked through the OVH provider, so qemu is purged at bake time; the later container-rebuild slow path runs on an already-purged OS and does not reinstall it.
- mngr continues to never order OVH automated backups when creating new OVH hosts; this is now guarded by a test.
- Existing already-running OVH hosts are not changed by this work (they will be recycled separately, picking up the purge then).
- The minds restic `BackupProvider` workflow is unaffected.

## Changes

- Add a `purge_qemu_packages` helper to `libs/mngr_ovh/imbue/mngr_ovh/bootstrap.py` that opens a root SSH session (reusing the existing connect/run-or-raise machinery) and runs a detect-then-purge command: only `apt-get purge --auto-remove -y 'qemu*'` when `dpkg -l | grep qemu` finds something, using `DEBIAN_FRONTEND=noninteractive` and the existing 300s apt timeout constant.
- Call the new helper from `OvhProvider._provision_vps` in `libs/mngr_ovh/imbue/mngr_ovh/backend.py` as the final post-rebuild bootstrap step, after `install_required_outer_packages`, so it runs on both the fresh-order and recycle paths and its failure is caught by the existing provisioning cleanup.
- Add a unit test (with a recording/fake `OvhVpsClient`) asserting the OVH order/cart configuration flow sets only the expected labels and never configures any backup option.
- Add a unit test for the new purge helper verifying the detect-then-purge command shape and that a non-zero remote exit raises `VpsProvisioningError`.
- Document in the OVH README that mngr purges qemu on each provisioned VPS to keep OVH automated backups disabled.
- Add a `mngr_ovh` changelog entry describing the user-visible change.
