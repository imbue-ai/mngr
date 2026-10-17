The desktop app no longer ships a test-only module to users.

`imbue/minds/utils/snapshot_container_states.py` exists only so the snapshot-resume CI suite can record what the pre-baked snapshot image captured; nothing the app runs imports it. Because its name did not mark it test-only, the wheel `exclude` rules did not catch it and it was packaged into every install. It is now `snapshot_container_states_testing.py` and named in the `exclude` list, so it stays in the checkout and out of the bundle.

A new wheel-contract check, `test_minds_wheel_excludes_test_only_helper_modules`, builds the minds wheel and fails if it contains any module whose name marks it test-only. The existing check only looked for test *files* (`test_*.py`, `*_test.py`, `conftest.py`), which is why this module went unnoticed.
