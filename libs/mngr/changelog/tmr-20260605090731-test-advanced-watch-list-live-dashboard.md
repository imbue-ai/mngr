Fixed the `test_advanced_watch_list_live_dashboard` e2e tutorial test. It
carried a spurious `@pytest.mark.modal` mark, but the `watch -n 5 mngr list`
dashboard command it exercises only performs Modal host discovery via the
in-subprocess gRPC SDK, which the resource guard cannot observe (the guard only
tracks the `modal` CLI binary or in-process gRPC). The mark therefore tripped
the guard's "marked modal but never invoked modal" check. Removed the mark and
strengthened the test to create a local agent and assert it appears in the live
dashboard output, rather than only checking that the `watch` wrapper exits.
