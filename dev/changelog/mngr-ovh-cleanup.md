Cleaned up dev-level files for the OVH-VPS removal.

- Removed `--backend slice` from the `bake-slice-{dev,prod}` justfile recipes (the flag no longer exists) and reframed the pool recipe comments to slice-only. The `destroy-pool-host` note still correctly states that `minds env destroy` tears down a whole env's unleased slices.

- Deleted the unused `scripts/remove_old_flat_vault_secrets.py`.

- Deleted obsolete specs/blueprints that only described the removed behavior: `specs/swap-pool-to-ovh/`, `blueprint/deprecate-ovh-vps/`, and `blueprint/disable-ovh-qemu-backups/`.
