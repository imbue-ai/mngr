Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`); the entries `mngr-per-client-host-keys.md` and `mngr-account-owned-providers-part-4.md` in this directory belong to the same PR.

- The README's SSH key material section names the per-instance `keys/known_hosts` store the code uses (it previously called it provider-wide).

- A provider instance's `token_id` and `token_secret` may come from different config layers (for example the id in `settings.toml` and the secret from the environment): the pairing is checked on the merged config when the instance is built, not per layer, which had rejected that split at startup.

- The client key that opens a sandbox resolves per host first, then to the instance's shared `keys/modal_ssh_key` when one is installed, and is never minted on a read path (`resolve_modal_client_keypair`): a sandbox this machine holds neither key for fails with `ModalHostKeyMissingError`, which names both paths and says the sandbox was created on another device or before per-host keys existed, so it must be reached from the device that created it or have its record synced here. Only `create_host` mints the per-host pair, so a connect no longer leaves behind a key the sandbox never authorized (one that would then have won over a shared key installed later); discovery lists such a sandbox from its record, at info level, instead of failing. The shared key is never created by mngr; it is the operator-installed key the TMR CI and mngr-seer debug flows have their sandboxes authorize, which the per-host-only resolver had stopped presenting.

- The per-host key layout is published as `MODAL_HOST_ACCESS_MANIFEST` (`constants.py`, with the key-file names `MODAL_SSH_KEY_NAME`, `MODAL_HOST_KEY_NAME` and `MODAL_KNOWN_HOSTS_NAME`), which the provider's own key dir and key paths resolve through; the deploy-time list of profile files kept out of images is built from the same names.

- The README's SSH key material section says that neither key is minted on a read path and that sandboxes created before the per-instance key dir existed are not reachable after upgrading: destroy and recreate them.
