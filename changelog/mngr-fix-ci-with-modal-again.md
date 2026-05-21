## Register snapshot-fixture Modal app for leak detection

`libs/mngr_modal/imbue/mngr_modal/routes/test_snapshot_and_shutdown.py`'s
`deployed_snapshot_function` fixture was registering only the test volume,
not the test app, so leaked snapshot apps were invisible to the session-end
leak detector in `pytest_sessionfinish`. Combined with `_stop_app` calling
`modal app stop` (which transitions the app to `stopped` but does not delete
it) and the fixture deploying to the default `main` environment, leaked
apps accumulated silently in `main` until they hit the workspace's deployed-
app cap.

This change adds `register_modal_test_app(app_name)` to the fixture setup
so the leak detector can surface any future failures.

This is a partial fix focused on detection. The underlying design issues
(`deploy_function(..., environment_name=None, ...)` deploying to `main`,
and `_stop_app` not actually deleting the app) are tracked separately and
require a larger refactor of the fixture's env routing.
