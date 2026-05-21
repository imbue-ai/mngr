## Register snapshot-fixture Modal app for leak detection

`libs/mngr_modal/imbue/mngr_modal/routes/test_snapshot_and_shutdown.py`'s
`deployed_snapshot_function` fixture was registering only the test volume,
not the test app, so leaked snapshot apps were invisible to the session-end
leak detector in `pytest_sessionfinish`. Combined with `_stop_app` calling
`modal app stop` (which transitions the app to `stopped` but does not delete
it) and the fixture deploying to the default `main` environment, leaked
apps accumulated silently in `main` until they hit the workspace's deployed-
app cap.

This change:

- Adds `register_modal_test_app(app_name)` to the fixture setup so the leak
  detector can surface any future failures.
- Hardens `_stop_app` and `_delete_volume` in the same file: explicit
  `--env main` (since the fixture deploys there), explicit `--yes` instead
  of feeding stdin, and a `logger.warning` when the subprocess returns
  non-zero so silent cleanup failures stop being invisible.

This is a partial fix focused on detection and visibility. The underlying
design issue -- `deploy_function(..., environment_name=None, ...)` deploying
to `main` and `modal app stop` only transitioning the row to `stopped` (not
deleting it) -- still requires a larger refactor that routes the fixture
through a per-test Modal env so `modal environment delete` can cascade the
app away on teardown.
