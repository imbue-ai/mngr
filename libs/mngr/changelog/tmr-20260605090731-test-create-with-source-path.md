Removed the inapplicable `@pytest.mark.modal` mark from the e2e tutorial test
`test_create_with_source_path`. The test creates a local agent via
`mngr create --from <path>`; its only modal contact is the incidental
discovery `mngr list` performs via the in-process modal SDK inside the `mngr`
subprocess, which the resource guard cannot observe. The mark therefore always
failed the guard's "marked modal but never invoked modal" check.
