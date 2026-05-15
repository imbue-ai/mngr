Add the `mngr_ovh` provider plugin: run mngr agents in Docker containers on OVH classic VPS instances (e.g. `vps-2025-model1` / "VPS-1" at ~$7.60/mo).

- Uses the official `python-ovh` SDK; supports OAuth2, AK/AS/CK, and `~/.ovh.conf` credentials.
- Provisions via the OVH `/order/cart` flow and bootstraps via `POST /vps/{s}/rebuild` with a pre-installed SSH public key (no cloud-init is available on OVH classic VPS).
- Discovers VPSes via OVH IAM v2 tags (`POST /v2/iam/resource/{urn}/tag`) on the `vps` resource URN, so multiple `mngr` instances on different machines see the same agents.
- First SSH connection performs a TOFU pin of the host key into a per-provider `known_hosts` file; strict host-key checking is enforced from then on. See `libs/mngr_ovh/README.md` for the security caveat.
- Refactors `VpsDockerProvider` to lift the shared parallel-SSH discovery into the base class behind a new `_list_provider_vps_ips()` abstract method; `mngr_vultr` now only contributes the tag-listing.
- Widens `os_id` in the VPS Docker base to `int | str` so providers (like OVH) can carry friendly image names through the existing build-args parser without disrupting integer-id providers (like Vultr).
