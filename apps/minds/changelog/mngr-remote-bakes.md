Pool-host docs now point at the canonical `minds pool create` flow (via the
new `just bake-pool-host` / `just list-pool-hosts` recipes) instead of the
low-level `mngr imbue_cloud admin pool create` recipe with hand-exported OVH
creds and a hand-passed `--management-public-key-file`. The env-aware wrapper
derives the management SSH key + OVH credentials from the activated tier's
Vault entries automatically; for staging/production it also resolves the
host_pool DSN from Vault.

Updated `docs/host-pool-setup.md` (steps 2, 5, dev workflow, cleanup) and
`docs/staging-bringup.md` (step 7) accordingly, and clarified that the baked
version comes from the `--workspace-dir` checkout, not from `--attributes`.
