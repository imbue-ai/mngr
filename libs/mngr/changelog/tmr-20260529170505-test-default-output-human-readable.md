Fixed the `test_default_output_human_readable` e2e tutorial test (covering `mngr ls`).
Removed the incorrect `@pytest.mark.modal`: this discovery-only command runs mngr as a
subprocess and never invokes the `modal` CLI binary that the resource guard's PATH wrapper
intercepts (read-only discovery skips the modal provider when no environment exists, and
otherwise reaches Modal only via the in-process Python SDK), so the mark caused a spurious
"marked with @pytest.mark.modal but never invoked modal" failure. Also strengthened the
assertions to verify the output is actually the human-readable default format rather than
just checking the exit code.
