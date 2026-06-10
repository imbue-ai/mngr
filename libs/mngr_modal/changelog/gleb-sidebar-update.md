Mark `test_exec_cwd_override_on_modal` as `@pytest.mark.flaky`. It hit
the same `modal.exception.NotFoundError: Lookup failed for Function
'snapshot_and_shutdown'` that 6 sibling tests in `test_exec.py` /
`test_modal_create.py` also exhibited in the same CI run -- those
recovered via the default 2-attempt retry; this one was unlucky and
failed both attempts. The `@flaky` marker lets offload retry it
longer while the underlying Modal sandbox-setup race is addressed
separately.
