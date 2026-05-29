Removed the spurious `@pytest.mark.modal` from the `test_full_lifecycle` e2e
test. The test drives a local `command` agent and only contacts Modal via
in-process gRPC discovery inside the `mngr` subprocess (invisible to the
resource guard) and never invokes the `modal` CLI binary, so the mark tripped
the guard's NEVER_INVOKED check. Added a comment documenting why the mark is
intentionally absent.
