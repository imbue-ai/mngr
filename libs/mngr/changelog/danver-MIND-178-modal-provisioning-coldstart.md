Added `wait_for_sshd_with_retry` function to `imbue.mngr.providers.ssh_utils` (MIND-178).

- New helper performs full SSH authentication and session-open check to verify sshd is fully ready, not just accepting connections.

- Used by mngr_modal to absorb cold-start latency where the tunnel is available but sshd is still initializing.
