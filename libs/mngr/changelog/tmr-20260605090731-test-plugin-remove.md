Strengthened the `mngr plugin remove` e2e tutorial test to assert that removing
a non-installed plugin fails cleanly with a user-facing "Aborted" error and
never a Python traceback, and added an unhappy-path test that verifies an
invalid package name is rejected with a clear argument-validation error.
