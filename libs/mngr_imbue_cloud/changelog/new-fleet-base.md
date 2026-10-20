`new-fleet-base` is the integration base for the in-progress slice-fleet generation-2 program (specs/slice-fleet-gen2 and specs/slice-fleet): the squash of the formerly stacked PRs #571, #573, #574, #581, #609, and #614. It is deployed only to dev canaries and must not be deployed or merged as-is; follow-up PRs stack on it and the whole program lands on `main` as one change.

For this project it carries:

- The generation-2 slice backend: `slices/qemu_slice.py` (the raw-qemu systemd template unit, the root helper with per-VM routed-tap nftables rules -- DNAT/SNAT, guest-to-box block, anti-spoofing, direct-to-MX SMTP block, connection ceilings, named counters, HTB fair share -- the exact-argument sudoers, one-time cloud-init material, the reserve / destroy / status scripts, and the in-guest sizing oneshots), `QemuSliceVpsClient` behind the new `SliceVmClientInterface`, and generation dispatch in `slices/slice_client.py` and `providers/rebuild.py`.

- Variable machine sizing: the units model and two-budget math in `slices/bare_metal.py`, `MachineUnits`, the units-based env schema and 512-ordinal ceiling, the `mngr imbue_cloud machines show` / `machines resize` commands, and the additive wire fields (`box_generation`, sizes, targets, machine quotas and usage).

- Management-plane plumbing: `BareMetalServer.wireguard_address` / `wireguard_public_key`, the destroy target's overlay address, `box_ssh_port` on slice clients and the `box_management_address` / `box_management_ssh_port` split on the slice provider config, and `-b generation=<n>` on `mngr create`.

The detailed per-phase history is in this directory's `mngr-design-network-observation`, `mngr-slice-fleet-gen2-phase-*`, `mngr-variable-sizing`, `mngr-new-fleet-testing`, `mngr-finish-new-fleet-canary-testing`, and `mngr-slice-fleet-canary-followups` entries.
