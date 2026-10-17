Gen-2 slice boxes, slice VMs, and workspace containers now trust a per-tier SSH certificate authority for management access instead of authorizing a static management key (imbue-ai/mngr-internal#850).

- New `gen2_scripts/ssh_ca.py`: the CA trust files every layer installs (`TrustedUserCAKeys`, an `AuthorizedPrincipalsFile` per account mapping the `mngr-operator` / `mngr-service` / `mngr-vm` / `mngr-container` principals) and the Vault mount / role names the signers use.

- The gen-2 slice carve passes the tier's CA public key (`trusted_user_ca_public_key` on the slice provider config) into the VM's cloud-init and no longer puts a static root key in it; containers on gen-2 slices get the same CA trust through the new `extra_ssh_config_files` hook, including rebuilt containers, which re-read it from the VM.

- `provision_slice_vm` takes an optional root authorized key plus the CA key; `count_authorized_keys` is replaced by `read_management_trust`, which also reports the pinned CA, and the tier-exclusivity check compares the authorized key count against the generation's expectation (one on gen-1, zero on gen-2) and requires the pinned CA to be the tier's.

- The gen-2 VM root no longer authorizes the bake machine's VPS key: the bake reaches gen-2 VMs with the same per-generation management credentials as everything else.
