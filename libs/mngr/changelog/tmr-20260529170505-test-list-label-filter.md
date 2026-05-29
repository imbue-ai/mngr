Fixed the `test_list_label_filter` e2e tutorial test: removed the unjustified
`@pytest.mark.modal` mark. `mngr list` is read-only and skips the Modal provider
(via `ProviderEmptyError`) when no Modal environment exists, so it never invokes
the `modal` CLI binary that the resource guard tracks -- the mark always tripped
the guard's superfluous-mark check. The happy-path assertion now verifies the
label filter matches nothing ("No agents found") in a fresh environment, and a
new `test_list_label_filter_invalid_format` test covers the unhappy path where a
label spec missing the `=` separator is rejected.
