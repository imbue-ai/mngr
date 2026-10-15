Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`); the entry `mngr-per-client-host-keys.md` in this directory belongs to the same PR.

- `create_host` combines main's instance-shape recording with the branch's leak-aware provisioning cleanup (`VpsInstanceLeakedError`).

- A VPS whose host key is not on this machine yet (a host created on another device) is logged at info during discovery rather than as a read failure on every poll.

- A per-host key dir that cannot be removed when its host is destroyed is logged at trace, as documented, instead of being silently ignored.

- When a VPS create fails after the instance exists and the rollback destroy fails too, the reported `VpsInstanceLeakedError` now names the provisioning failure (cloud-init timeout, host-key mismatch, sshd never up) and chains it as its cause, instead of replacing it with only the leak message.

- The per-host key layout is published as `VPS_HOST_ACCESS_MANIFEST` (`primitives.py`), which the provider's own key dir resolves through (its per-host `known_hosts` link target is the container placement's store; bare placement links `vps_known_hosts` itself); the container key-file names (`CONTAINER_SSH_KEY_NAME`, `CONTAINER_HOST_KEY_NAME`, `CONTAINER_KNOWN_HOSTS_NAME`) moved from `docker_realizer.py` to `primitives.py` beside the VPS ones.

- `resolve_vps_root_keypair` returns a `VpsRootKeypair` (the key path, its public half, and whether the retired provider-wide key was used) instead of a tuple, and a provider instance reports opening a host with that legacy key once per process at info, naming the instance (`is_legacy_key_fallback_logged`), instead of a debug line on every call. `VpsHostKeyMissingError` carries help text saying the host was created on another device or before per-host keys existed, so it must be reached from the device that created it or have its record synced here.

- The README's manifest paragraph says the published layout is the container-placement one and that minds materializes container placement only; the `mngr-per-client-host-keys` entry's destroy claim is corrected: a host this machine already knows but holds no key for is still destroyed through the cloud API, while a running VPS whose key is absent here is not listed here and is destroyed from the device that holds its key.
