Variable machine sizing (specs/slice-fleet, phase 1). The tier `deploy.toml` `[plans]` blocks gain the two machine-sizing quotas (`max_active_machine_units`: free 8 / explorer 16 / ally 80; `max_total_machine_disk_gb`: free 140 / explorer 280 / ally 1400), written into the connector's plans table on deploy.

New opt-in release test (`deployment_tests/test_machine_resize.py`, `MINDS_MACHINE_RESIZE_RELEASE_TEST=1`): leases a default machine, records a units + disk resize, restarts through stop/start, and verifies the applied size from both the connector's recorded state and the machine itself over SSH (visible RAM + the grown data filesystem).

Docs: the new `specs/slice-fleet/spec.md` supersedes `specs/slice-fleet-gen2/spec.md` (pointer header added); glossary gains a "machine size" entry; host-pool-setup and workspace-stop-start document the two-budget accounting, eviction, and ordering validation.

This branch also carries the earlier slice-fleet gen-2 phase 1-4 work; see the `mngr-slice-fleet-gen2-phase-*` and `mngr-variable-sizing` entries in this same PR.
