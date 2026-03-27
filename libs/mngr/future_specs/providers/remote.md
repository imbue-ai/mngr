# Remote Provider Spec [future]

## SSH Access

Per-host ssh keys are stored in `~/.ssh/mngr/$HOST_ID` and deleted when the host is destroyed. If no key exists, `mngr` assumes access is configured via `~/.ssh/config` or agent forwarding.

## Heartbeat Mechanism

Remote environments need a heartbeat script baked into the container to detect when the controlling `mngr` process dies unexpectedly.

Without this mechanism, the following failure scenario could occur:
1. `mngr` starts a remote host
2. The host starts successfully and agents begin running
3. The outer `mngr` process crashes or the network connection is lost
4. The remote host continues running indefinitely, unaware that it should time out

To prevent this, a heartbeat script must be provisioned into the remote environment during host creation. This script:
- Periodically checks for a heartbeat signal from the controlling process
- Times out and stops the host if no heartbeat is received within a configured interval
- Acts as a safety mechanism to ensure remote hosts don't run indefinitely when orphaned

The heartbeat mechanism should be coordinated with the idle detection system to avoid conflicting shutdown triggers.
