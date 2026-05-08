- mngr_modal: restore the per-test reset of `ModalProviderBackend._app_registry`. The
  autouse `_reset_modal_app_registry` fixture was deleted in #1533. After #1522
  reshaped the test factory to dispatch through `_construct_modal_provider`
  (which short-circuits on the class-level `_app_registry`), the reset became
  load-bearing for cross-test isolation: the second test in a worker would
  reuse the first test's cached app and skip `modal_interface.app_create(...)`,
  leaving `testing_modal._apps` empty and breaking helpers like
  `make_sandbox_with_tags`. Restoring the fixture fixes the post-merge CI
  failures on main.
