Phase 2 of the user-controlled-keys plan: per-host SSH keys for Modal sandboxes.

New sandboxes get their own client keypair (`modal_ssh_key`) and sshd host keypair instead of the provider-wide shared pair; the host key is re-injected at every sandbox boot (snapshot restores included), and a clone (`create_host --snapshot` under a fresh host_id) mints fresh keypairs rather than inheriting the source host's. Connections to sandboxes created before this change fall back to the legacy shared pair, which is CLEANUP-marked for removal once those sandboxes cycle out.

All known_hosts pins now carry the host_id and are written through mngr core's new host-key pin store, and `delete_host` forgets the host's pins and per-host keypairs. The unused `get_ssh_public_key` accessor is removed.
