Per-tier SSH certificate authority for gen-2 slice-fleet management (imbue-ai/mngr-internal#850).

- `deploy.toml` gains an optional `[ssh_ca] public_key` block (the tier's Vault `ssh-<tier>` CA public key, which every gen-2 box, VM, and container pins) and an `ssh-ca` Vault-backed service holding the connector's AppRole credentials (`VAULT_SSH_APPROLE_ROLE_ID` / `VAULT_SSH_APPROLE_SECRET_ID`; template `.minds/template/ssh-ca.sh`).

- The Vault reader can sign an SSH public key through a mount's role and read a mount's CA public key.

- Docs: the Vault setup doc describes the CA, its roles, and the operator bring-up; the gen-2 management-plane, host-pool, tier-bringup, pool-hosts, environments, services, telemetry, and cutover docs describe certificate-based management access, the new audit fields (`expected_authorized_key_count`, `is_trusted_ca_correct`), and the operator identity directory `~/.mindsadmin/<tier>/` (WireGuard key and SSH key). The next-deploy checklist carries the rollout order, including repaving the existing gen-2 dev canary boxes.

- The dev-time desktop bundle lock (`electron/pyproject/uv.lock`) records `imbue-mngr`'s paramiko floor moving to 3.2 for the certificate-aware SSH key loader.
