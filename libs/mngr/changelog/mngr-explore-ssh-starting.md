- Container hosts now self-heal sshd after an out-of-band restart. The host
  container entrypoint (re)starts sshd on boot whenever mngr has already
  provisioned this host (tracked by a marker, so a host key pre-baked into the
  base image is never used by mistake), so a `docker restart`, docker daemon
  restart, or host reboot brings ssh back without waiting for `mngr start`.
  mngr's own sshd start is now idempotent (a no-op when sshd is already running).

- `mngr start` is now safe to run concurrently for the same host. The agent
  (re)launch is serialized by a dedicated cross-actor `flock` (local in-host
  starts coordinate with remote over-SSH starts), so a desktop-driven start and
  an in-host boot-hook start cannot race. The lock blocks until acquired (wrap
  the command in `timeout` for a deadline).

- Added a `--post-host-create-outer-command` create option (and matching
  create-template / settings key `post_host_create_outer_command`). It runs
  shell commands once on the host's outer machine (the underlying VM/daemon
  host) after the host is created -- e.g. to install a VM-level systemd unit.
  Skipped with a warning when the provider exposes no outer host.
