- Fixed the `test_templates_setup_via_config_edit` e2e tutorial test: added the
  missing `@pytest.mark.timeout(120)` override (it was falling back to the global
  10s default and timing out during CLI cold start) and removed the stale
  `@pytest.mark.modal` mark (the test substitutes the template with
  `transfer = "none"` and never actually exercises Modal).

- Strengthened the same test to verify concrete effects rather than only exit
  codes: it now confirms `mngr config edit` created the project config from its
  template, and that the applied template runs the agent in-place (work directory
  equals the session cwd).
