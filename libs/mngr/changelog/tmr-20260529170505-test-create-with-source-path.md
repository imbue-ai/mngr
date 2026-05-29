Removed the superfluous `@pytest.mark.modal` mark from the e2e release test
`test_create_with_source_path`. The test exercises a purely local command
agent (`mngr create --from <path>`), which never invokes Modal, so the
resource guard failed the otherwise-passing test with a "marked modal but
never invoked modal" violation.
