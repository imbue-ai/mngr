Fixed the `mngr list` e2e release tests (`test_list_with_no_agents`,
`test_list_json_with_no_agents`). They now get an explicit per-test timeout
(the global 10s limit was too tight for Modal provider discovery over the
network) and no longer carry the incorrect `@pytest.mark.modal` mark. Those
tests run `mngr` as a subprocess that reaches Modal only through the Python
SDK's gRPC client, never the `modal` CLI binary that the resource guard
tracks, so the mark made the guard fail them with "marked with
@pytest.mark.modal but never invoked modal". The `@pytest.mark.release` mark
already restricts them to the Modal-capable release CI.
