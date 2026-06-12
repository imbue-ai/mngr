Hardened reverse SSH tunnel teardown so a half-dead connection no longer orphans the forwarded port on the remote sshd (which made the next run's port forward request get denied):

- `SSHTunnelManager.cleanup()` now attempts to cancel each reverse port forward unconditionally, instead of skipping the cancel when the connection looks inactive.

- Every tunnel SSH connection now sends periodic keepalives, so an idle reverse tunnel does not silently die unnoticed and the health check can repair a dead connection promptly.
