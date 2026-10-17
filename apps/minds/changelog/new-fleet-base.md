`new-fleet-base` is the integration base for the in-progress slice-fleet generation-2 program (specs/slice-fleet-gen2 and specs/slice-fleet): the squash of the formerly stacked PRs #571, #573, #574, #581, #609, and #614. It is deployed only to dev canaries and must not be deployed or merged as-is; follow-up PRs stack on it and the whole program lands on `main` as one change.

For this project it carries:

- The per-tier `management_plane.toml` (operator WireGuard public keys and addresses, the tier's Modal Proxy name, static IPs, and Modal environment), its loader and models, the per-tier management-overlay allocation table (`MANAGEMENT_OVERLAY_CIDR_BY_TIER`, disjoint carves of `10.64.0.0/10`), and the dev tier's filled-in config.

- The tier `deploy.toml` `[plans]` blocks' two machine-sizing quotas (`max_active_machine_units`, `max_total_machine_disk_gb`).

- The read-only machine-size display on the workspace settings page, backed by `GET /ui/api/workspaces/<id>/machine-size` (over `mngr imbue_cloud machines show`).

- Operator runbooks: `docs/deploy/gen2-management-plane.md` (lockdown, WireGuard, onetun transport, `server ssh`, break-glass), `docs/deploy/gen2-telemetry.md`, and the two-budget accounting / eviction / ordering-validation updates to `host-pool-setup.md` and `workspace-stop-start.md`; a "machine size" glossary entry; the release checklist's trixie image pin reminder.

- Two opt-in release tests: `deployment_tests/test_machine_resize.py` and `deployment_tests/test_machine_migration.py` (the latter covers the in-supervisor gen-1 -> gen-2 conversion, which the follow-up work replaces with a one-time migration).

The detailed per-phase history is in this directory's `mngr-slice-fleet-gen2-phase-*`, `mngr-variable-sizing`, `mngr-new-fleet-testing`, `mngr-finish-new-fleet-canary-testing`, `mngr-slice-fleet-canary-followups`, `mngr-onetun-install-and-canary`, and `mngr-machine-size-display` entries.
