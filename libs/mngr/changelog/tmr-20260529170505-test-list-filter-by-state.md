Fixed the `test_list_filter_by_state` e2e test so it runs under release. It
was hitting the default 10s function timeout and carried a superfluous
`@pytest.mark.modal` mark (the test only exercises local command agents, so
the Modal resource guard flagged it as never invoking Modal). Added
`@pytest.mark.timeout(300)`, dropped the `modal` mark, and strengthened the
assertions to verify the authoritative agent `state` field returned by
`mngr list --stopped`/`mngr list`, not just name membership.
