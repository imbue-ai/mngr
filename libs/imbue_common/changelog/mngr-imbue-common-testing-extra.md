`imbue-common` gains a `testing` extra -- `import-linter`, `inline-snapshot`, `pytest` -- declaring
what a downstream test suite needs to use the test library the wheel ships
(`imbue.imbue_common.ratchet_testing`, and now `imbue.imbue_common.pytest_utils`, which was
excluded from the wheel). `ratchet_testing` imported `import-linter` with no dependency naming it,
so it only worked for consumers that happened to install `import-linter` themselves. This repo's
own pytest conventions (`conftest_hooks`) stay out of the wheel, so a plain install still pulls in
no pytest. `build_test.py` installs the built wheel with `[testing]` into a scratch venv and imports
the test library.
