Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`).

- The per-VM key layout (`keys/hosts/<host id>/` holding `ssh_host_ed25519_key` and `root_ssh_key` with their public halves and the VM's own `known_hosts`) is published as `LIMA_HOST_ACCESS_MANIFEST` (`constants.py`, with the key-file names `LIMA_HOST_KEY_NAME`, `LIMA_ROOT_SSH_KEY_NAME` and `LIMA_KNOWN_HOSTS_NAME`), which the provider's own key dirs resolve through, so the minds workspace record carries a VM's access material without restating the layout.
