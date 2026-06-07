Consolidated host-level provisioning into a single source of truth. A new
`host_setup.py` module defines the ordered, idempotent, config-gated setup steps
(pinned Docker install, optional gVisor `runsc` install, sshd `MaxSessions` /
`MaxStartups` tuning, base packages, and an optional qemu purge). `cloud_init.py`
now renders its first-boot `runcmd` block from those same steps, and a new
`apply_host_setup_on_outer()` runs the identical steps over SSH so a host can be
re-provisioned consistently after first boot.

Docker is now pinned to an exact version (29.5.1 on Debian 12) and installed via
the official Docker apt repo with `--allow-downgrades`, so provisioning is
reproducible and a re-run upgrades/downgrades an old host to match (replacing the
unpinned `get.docker.com | sh` install). gVisor `runsc` is pinned to a dated
release and downloaded + checksum-verified directly.

The SSH host-key injection stays first-boot-only in the cloud-init wrapper and is
deliberately excluded from the re-runnable steps, so re-provisioning never resets
the VPS host key or breaks `known_hosts`.

Made `start_container` (shared by vps_docker / ovh / lima) resilient to restarting
a container under gVisor (runsc). A leftover runsc sandbox from the container's
previous run can keep the rootfs-overlay `.gvisor.filestore` mounted, so
`docker start` fails with "repeated submounts are not supported with overlay
optimizations". `start_container` now runs the start + recovery + retry as a
single remote script: on that specific gVisor error it reaps the leftover runsc
processes scoped to that container id, removes the stale on-disk filestore, then
retries. A normal start stays a single `docker start`.
