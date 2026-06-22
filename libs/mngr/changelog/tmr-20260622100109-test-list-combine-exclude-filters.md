Fixed the LABELS tutorial e2e tests so a bare `mngr list` no longer aborts when an un-credentialed cloud backend (e.g. AWS) or an unreachable Docker daemon happens to be installed. The e2e fixture now scopes provider discovery to exactly the backends each test declares via its marks (`local` always; `docker`/`modal` only when the test carries the matching mark), making the fixture's documented "only local/docker/modal" contract actually hold.

Also dropped the spurious `@pytest.mark.rsync` from `test_list_combine_exclude_filters`: it creates only local command agents, which never invoke rsync.

Strengthened `test_list_combine_exclude_filters` to assert the listing's `errors` field is empty, so a partial provider-discovery failure can no longer let the exclusion assertion pass for the wrong reason.
