Removed the superfluous `@pytest.mark.modal` from the e2e release test
`test_create_with_extra_tmux_windows`. The test creates a local-provider agent
and never invokes Modal, so the resource guard flagged the mark as never-invoked.
This matches the other local-provider create e2e tests, which are not Modal-marked.
