Removed the superfluous `@pytest.mark.modal` from the e2e tutorial test
`test_list_custom_human_format` (output-formats section). The test runs
`mngr list --format '{agent.name} ({agent.state})'`, which discovers Modal
only via the Python SDK (gRPC) inside the `mngr` subprocess and never shells
out to the `modal` CLI. Because the resource guard can only observe Modal CLI
invocations from a subprocess (the SDK monkeypatch runs only in the pytest
process), the mark could never be satisfied and the guard failed the test with
"marked with @pytest.mark.modal but never invoked modal". No behavior change to
mngr itself.
