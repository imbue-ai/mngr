Fixed the `test_list_format_json_recap` e2e tutorial test for `mngr list --format json`:
it now carries a `@pytest.mark.timeout(120)` (the bare command needs ~9.5s just for
mngr cold-start, exceeding the default 10s pytest timeout once Modal discovery runs),
and the unenforceable `@pytest.mark.modal` mark was removed (Modal discovery happens
via the in-process SDK inside the mngr subprocess, which the resource guard cannot
observe, so the mark always tripped the "marked but never invoked" check). The test
also now parses and validates the JSON output structure rather than only checking the
exit code.
