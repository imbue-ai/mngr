Fixed a bug where restarting a stopped `imbue_cloud` (leased pool) mind left it in a broken, unrecoverable state. The subsequent `mngr start` SSH into the container failed ("Start step of host restart failed"), leaving the mind dead and UI-unrecoverable even though its data was intact on the volume.

There were two parts to the fix:

- `ImbueCloudProvider.get_host` previously returned an online host unconditionally, without checking whether the inner container was actually running. Because `mngr start` only re-starts a host when `get_host` reports it offline, the start command skipped `start_host` entirely and SSHed straight into the dead container. `get_host` now probes the container's running state over the outer root SSH (mirroring `VpsDockerProvider.get_host`) and returns an offline host when the container is stopped, so `mngr start` routes through `start_host`.

- `start_host` previously did a bare `docker start` and returned. But the in-container sshd is launched via `docker exec` (the container's command is just a sleep), so it does not survive a stop/start — the container came back with no sshd, and the per-host authorized key and host key may not have persisted either. `start_host` now re-bootstraps the container's SSH over the outer root SSH (which works independently of the container's sshd): it relaunches sshd, re-seeds the per-host authorized key (in case `/root` did not persist), waits for sshd to accept connections, and re-scans and re-records the served host key (reconciling any host-key change so strict host-key checking succeeds). This mirrors what the local docker and vps-docker providers already do on restart.

Together, a stopped leased mind can now be brought back to life.
