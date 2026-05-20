Add the `mngr_ovh` provider plugin: run mngr agents in Docker containers on OVH classic VPS instances (e.g. `vps-2025-model1` / "VPS-1" at ~$7.60/mo).

- Uses the official `python-ovh` SDK; supports OAuth2, AK/AS/CK, and `~/.ovh.conf` credentials.
- Provisions via the OVH `/order/cart` flow and bootstraps via `POST /vps/{s}/rebuild` with a pre-installed SSH public key (no cloud-init is available on OVH classic VPS).
- Discovers VPSes via OVH IAM v2 tags (`POST /v2/iam/resource/{urn}/tag`) on the `vps` resource URN, so multiple `mngr` instances on different machines see the same agents.
- First SSH connection performs a TOFU pin of the host key into a per-provider `known_hosts` file; strict host-key checking is enforced from then on. See `libs/mngr_ovh/README.md` for the security caveat.
- `mngr create --provider ovh` automatically reuses a cancelled-but-still-alive OVH VPS (the leftover from a prior `mngr destroy` that OVH won't actually decommission until end of month) instead of ordering a fresh one. Controlled by `enable_recycle_cancelled` (default `True`), `recycle_safety_margin_hours` (default `24`), and `recycle_max_candidates_considered` (default `10`).
- Adds `mngr ovh list [--all]` operator command: shows every mngr-tagged OVH VPS in the account (or every VPS with `--all`) with plan, datacenter, state, expiration, cancellation status, and IAM tags (`mngr-provider`, `mngr-host-id`, `mngr-recycling-by`). Plain text table; one IAM-resource call plus parallel per-VPS detail fetches via `ConcurrencyGroupExecutor`.
