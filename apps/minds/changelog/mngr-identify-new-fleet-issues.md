Pre-cutover fleet fixes (`blueprint/pre-cutover-fleet-fixes/`), docs.

- `host-pool-setup.md` gains a "Box maintenance" section (`server drain` -> maintenance -> `server undrain`).

- `gen2-cutover.md`'s per-tier prerequisites include the staging/production management-plane lockdown steps (Modal proxy, per-tier `management_plane.toml`, connector deploy, then box prep), gated on imbue-ai/mngr-internal#850.

- `next_deploy.md` covers connector migration 040 (uplink NOT NULL; deploy minds-admin and the connector together) and lists the staging and production lockdown as must-happen items.
