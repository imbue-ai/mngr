## libs/mngr

- e2e: tightened assertions in `test_create_with_template_modal_disabled`. The
  test now rejects raw tracebacks, requires the error to reference `modal`
  specifically (proving the template was applied), and verifies that no
  agent leaked into `mngr list` after the failed create.
