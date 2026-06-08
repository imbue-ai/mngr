- e2e tests: fixed the PROJECTS tutorial test `test_list_project_dot` by removing the
  superfluous `@pytest.mark.modal` mark. `mngr list --project .` runs in a subprocess
  and only touches Modal via the SDK (gRPC), which the resource guard cannot track from
  the subprocess (it only tracks the Modal CLI there), so the mark tripped the guard's
  "marked but never invoked" check. Also strengthened the test to assert the command
  emits a clean "No agents found" listing, confirming `.` is expanded to the current
  project rather than rejected or treated literally.
