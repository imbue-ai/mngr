Fixed the `test_create_with_project_label` e2e tutorial test, which was marked
`@pytest.mark.modal` but never invoked the modal binary (provider discovery uses
the modal Python SDK, not the guarded binary), causing the resource guard to fail
it. Removed the superfluous mark and added a companion `test_create_default_project_label`
that verifies the default `project` label is derived from the git repo folder name
when `--project` is omitted.
