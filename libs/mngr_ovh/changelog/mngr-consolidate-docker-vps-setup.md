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
