mngr: `cleanup_old_modal_test_environments` now reports which environments it actually reaped.

It used to return the number of environments it *attempted*, discarding the per-environment outcome it had already computed. A sweep that failed to delete a single environment was indistinguishable from one that deleted them all, so the CI job driving it could never go red.

It now returns a `ModalTestEnvironmentSweepResult` naming the environments that are gone and the ones whose delete failed. The per-environment work moved into `sweep_old_modal_test_environment`, which is injectable (along with the environment lookup) so the sweep can be unit-tested without invoking the real Modal CLI.

Separately, the Modal-specific code that had accumulated in core moved out to the `mngr_modal` plugin: `imbue.mngr.utils.modal_cli`, the "Modal test environment cleanup utilities" section of `imbue.mngr.utils.testing`, and `ModalCliOutputError`. Core keeps the provider-agnostic leak-tracking registries. If you imported any of those from `imbue.mngr`, import them from `imbue.mngr_modal.modal_cli` / `imbue.mngr_modal.cleanup` / `imbue.mngr_modal.errors` instead.

