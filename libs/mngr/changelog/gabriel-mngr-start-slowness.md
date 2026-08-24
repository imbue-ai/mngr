SSH liveness hardening, from the `mngr start` slowness investigation (`specs/mngr-start-slowness-investigation.md`):

- Every SSH connection mngr establishes (pyinfra host connections and outer hosts) now enables paramiko transport keepalives, so a silently dead network path (laptop slept mid-operation, dropped NAT state) surfaces as a transport error instead of leaving blocked reads waiting forever.

- Channel opens are now bounded: the cooperative host-lock channel, the detached debug lock holder, and SFTP channel creation all pass an explicit open timeout, and SFTP channels get a default per-read silence timeout, so an sshd that accepts TCP but no longer services channels can no longer hang these paths indefinitely. The detached lock holder's launch confirmation is bounded too, and a stall there degrades to the same tolerated "did not confirm launch" warning as a channel that closes early. The cooperative lock *wait* is deliberately left unbounded -- that one is waiting on another actor's work, not on a dead path.

- Container sshds launched via `SSHD_START_OPTIONS` (the docker provider, vps container setup, and imbue_cloud slice adoption; existing containers pick it up on their next sshd relaunch) now run with `ClientAliveInterval 30` / `ClientAliveCountMax 4`: a client whose path died silently is reaped in about two minutes instead of the kernel TCP keepalive's ~2h11m, which is what let a dead cooperative-lock holder block every subsequent `mngr start` for hours.

- `get_default_cli_events_log_dir` added to `imbue.mngr.utils.logging` so consumers (the minds bug-report attachment sweep) can locate the per-command CLI event log without loading a logging config.
