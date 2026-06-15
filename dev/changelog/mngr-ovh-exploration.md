Added a design doc (`blueprint/ovh-baremetal-slices/`) for extending the imbue_cloud pool to allocate "slices" (lima/QEMU VMs) on rented OVH bare-metal servers as an alternative to ordering OVH VPSes, including the data model, admin lifecycle, connector release fork, and a recorded pricing gotcha (catalog base price excludes RAM/storage upgrades).

Added a refactor design doc (`blueprint/mngr-imbue-cloud-module-layers/`) proposing a layered sub-package structure for the `mngr_imbue_cloud` plugin (with an `import-linter` ordering contract), isolating the slice/bare-metal subsystem and the pool-bake code into their own layers and decomposing the oversized `instance.py`.

Added an `import-linter` "mngr_imbue_cloud layers contract" (root `pyproject.toml`) and a `test_meta_ratchets.py` test that enforces it, as part of restructuring the `mngr_imbue_cloud` plugin into layered sub-packages.
