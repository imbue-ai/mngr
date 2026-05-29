Fixed the `test_list_format_jsonl` e2e tutorial test, which was failing the
resource guard with "marked with @pytest.mark.modal but never invoked modal".
A pure `mngr list` reaches Modal only through the Python SDK in the `mngr`
subprocess, which the resource guard cannot observe (only Modal CLI invocations
are tracked across processes via the PATH wrapper), so the `modal` mark could
never be satisfied. Removed the inapplicable mark and strengthened the test to
assert that the output is valid JSONL.
