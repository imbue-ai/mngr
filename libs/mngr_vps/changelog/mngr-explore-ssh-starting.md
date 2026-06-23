- The agent container's PID-1 entrypoint now self-heals sshd: on every
  (re)start it restarts sshd once mngr has provisioned a host key, so the
  container is reachable again after a VM reboot or `docker restart` without
  waiting for `mngr start`. The explicit sshd (re)start used during setup and
  after a container restart is now idempotent (a no-op when sshd is already up).

- Register the gVisor (runsc) runtime with `--overlay2=none` so a container's
  writable layer is written through to the persistent Docker overlay2 layer and
  survives a `docker stop`/`start` or host reboot. Previously runsc used its
  default per-sandbox overlay (`--overlay2=root:self`), which is recreated on
  every start, so the injected sshd host key, the `/mngr` host_dir symlink, and
  mngr's provisioning markers were silently lost on restart -- leaving the
  container unreachable until mngr re-provisioned it. Applies to every provider
  that installs runsc via the shared VPS host-setup (aws, vultr, ovh, gcp,
  azure, imbue_cloud).

- Removed the now-dead gVisor self-overlay filestore-collision recovery from
  `start_container` (the reap-and-retry path only existed for the `root:self`
  overlay that `--overlay2=none` eliminates); `start_container` is now a plain
  `docker start`.
