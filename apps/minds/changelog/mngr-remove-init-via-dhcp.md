Docs for the gen-2 DHCP placement change (imbue-ai/mngr-internal#849), under which a gen-2 slice's cloud-init runs exactly once and a restore onto another ordinal or box never replays it.

- `docs/deploy/host-pool-setup.md` gains a "Networking on a gen-2 box" section (the per-slice routed taps, the box-side dnsmasq DHCP server and its config/unit/lease-file locations, why the cidata is written once and copied verbatim by restores, and how the server is confined: the unprivileged `mngr-dhcp` user, the systemd sandbox and its single `CAP_NET_BIND_SERVICE` capability, and the box-level policy keeping udp/67 tap-only). `docs/deploy/gen2-management-plane.md` notes the `nftables.service` persistence plumbing is shared with that policy.

- `docs/deploy/gen2-cutover.md` prerequisites and `docs/deploy/next_deploy.md` add the re-prep every gen-2 box needs before restores land on it, the restore-onto-a-different-ordinal verification (adopted host key unchanged, `cloud-init status` shows no rerun, root's `authorized_keys` unchanged), and the retirement of the pre-DHCP dev gen-2 slices, whose static netplan cannot follow a placement change.

- The glossary's adoption entry marks the every-boot cloud-init replay the reconciler heals as a gen-1 behavior.

- `docs/deploy/gen2-telemetry.md` lists the slice DHCP server's config, unit and udp/67 policy among the prep artifacts the integrity check watches.
