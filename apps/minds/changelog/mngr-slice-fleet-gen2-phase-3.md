Slice-fleet-gen2 phase 3 (management-plane lockdown machinery):

- New per-tier `management_plane.toml` (committed under `imbue/minds/config/envs/<tier>/`, loader + models in `imbue/minds/config`): the tier's operator WireGuard public keys/addresses and its Modal Proxy name + static IPs -- all public values, PR-reviewed. The dev tier ships a commented scaffold; a tier without the file has no management plane configured.

- New operator runbook `docs/deploy/gen2-management-plane.md`: bring-up ordering, peer changes, rollback, and the break-glass paths (OVH rescue console; a Modal-sandbox bounce host behind the same proxy). The release checklist now carries a reminder to bump the DWT trixie image pin and the gen-2 slice guest image pin together.
