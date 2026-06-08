Fixed the `test_archive_stopped_via_stdin` e2e tutorial test: removed the
incorrect `@pytest.mark.modal` marker (the test only exercises local lifecycle
commands and never invokes the Modal CLI, so the resource guard failed the
otherwise-passing test) and strengthened it to verify the archive's actual
effect (the `archived_at` label is applied, the agent appears under
`mngr list --archived`, and is filtered out of `mngr list --active`).
