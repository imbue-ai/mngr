Fixed: Harden Modal host bring-up against cold-start and provider-discovery latency (MIND-178).

- Added tenacity retry to `_get_ssh_info_from_sandbox` to absorb transient `ModalProxyError` during sandbox tunnel setup.

- Added `wait_for_sshd_with_retry` helper to verify the SSH server can actually open sessions (not just accept connections), preventing "No existing session" errors from cold-started sandboxes.
