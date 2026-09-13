`management_plane.toml`'s `[modal_proxy]` block gains an optional `environment_name`: the Modal environment the tier's shared proxy lives in. Needed because proxy lookup is Modal-environment-scoped while a workspace holds at most one proxy, so per-env tiers (dev) keep the shared proxy in one environment (`main`).

The dev tier's `management_plane.toml` is now filled in (gen-2 management-plane activation): the `minds-dev-connector` Modal proxy (environment `main`, static egress IP allowlisted for box `:22`) and Josh's operator WireGuard public key at 10.202.0.2.

The machine-resize release test raises the test account's machine-size entitlements (through the pool DSN, the quota-enforcement test's precedent) before recording the resize: a fresh account's plan allows exactly the default 8-unit machine, so the 16-unit resize was refused by the very quota machinery under test.

New opt-in migration release test (`test_machine_migration.py`, `MINDS_MACHINE_MIGRATION_RELEASE_TEST=1`): leases a gen-1 machine, plants a workspace marker, force-restores it off its origin box, and asserts the gen-1 -> gen-2 conversion end to end (gen-2 placement, trixie guest, byte-identical host key, surviving marker, stamped sizes). Phase 3 of specs/slice-fleet enables it against the CI split fleet.
