- The agent container's PID-1 entrypoint now self-heals sshd: on every
  (re)start it restarts sshd once mngr has provisioned a host key, so the
  container is reachable again after a VM reboot or `docker restart` without
  waiting for `mngr start`. The explicit sshd (re)start used during setup and
  after a container restart is now idempotent (a no-op when sshd is already up).
