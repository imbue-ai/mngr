Removed the legacy OVH-VPS pool-host path from the minds env tooling. Pool hosts are bare-metal slices only.

- `minds pool create` is slice-only: dropped `--backend` and the OVH-VPS-only flags; `--server-id` is required.

- `minds env destroy` no longer tags/terminates OVH VPSes (deleted the `envs/providers/ovh_tags.py` module and its env-teardown step).

- Stopped reading the `<tier>/ovh` Vault entry during deploy and dropped the `ovh` Modal secret from the connector deployment. The `<tier>/ovh` Vault entry and `.minds/template/ovh.sh` remain, reframed for operator-sourced bare-metal box ordering (`mngr imbue_cloud admin server`).

- Dropped the now-unused `imbue-mngr-ovh` dependency from the minds package (the shipped Electron bundle keeps it, since `mngr_imbue_cloud`'s bare-metal box ordering still uses it).

The direct OVH provider (`mngr create @host.ovh`) is unaffected.
