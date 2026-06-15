`just bake-pool-host-dev` now passes `--skip-deferred-install-wait` so dev pool bakes don't wait the extra few minutes for the deferred Playwright/apt install before stopping the services agent.

Replaced the `just bake-pool-host` recipe with `just bake-pool-host-dev` (bake from a working tree -- best-effort branch label) and `just bake-pool-host-prod` (clone an exact FCT tag -- strict), reflecting that the imbue_cloud pool bake now derives the stamped repo identity from its source rather than from hand-typed `--attributes`. The `minds-justfile` skill documents the dev-vs-production distinction and how to set the create form's repository for a fast-path match.

Added a `just minds-install` recipe that installs the minds desktop client's node deps (electron, etc.) using the Node version pinned in `apps/minds/.nvmrc` (selected via `select_node_version.sh`), so the install no longer fails with `ERR_PNPM_UNSUPPORTED_ENGINE` when the shell's default node has drifted off the pin. `just minds-start`'s "not installed yet" hint now points at `just minds-install` (instead of a raw `cd apps/minds && pnpm install`, which skipped the node selection and hit the engine check).

Added a design doc (`blueprint/ovh-baremetal-slices/`) for extending the imbue_cloud pool to allocate "slices" (lima/QEMU VMs) on rented OVH bare-metal servers as an alternative to ordering OVH VPSes, including the data model, admin lifecycle, connector release fork, and a recorded pricing gotcha (catalog base price excludes RAM/storage upgrades).

Added a refactor design doc (`blueprint/mngr-imbue-cloud-module-layers/`) proposing a layered sub-package structure for the `mngr_imbue_cloud` plugin (with an `import-linter` ordering contract), isolating the slice/bare-metal subsystem and the pool-bake code into their own layers and decomposing the oversized `instance.py`.

Added an `import-linter` "mngr_imbue_cloud layers contract" (root `pyproject.toml`) and a `test_meta_ratchets.py` test that enforces it, as part of restructuring the `mngr_imbue_cloud` plugin into layered sub-packages.

Bumped the per-test timeout on the `test_cli_docs_are_up_to_date` meta-ratchet test: the enlarged imbue_cloud CLI surface (the new `admin server` + slice commands) made full CLI-doc regeneration exceed the default 10s pytest-timeout in the slower offload sandbox.
