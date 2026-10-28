`docs/testing-overview.md` and `docs/deploy/ops/app-release.md` no longer say the mngr Release Tests workflow runs on `v*` tag pushes: the mngr release pipeline now runs it before publishing.

`test_materialize_names_a_detached_tree_by_its_commit` is marked flaky: it hit its 10s timeout on a local `git checkout --detach` in one offload run and passes in under a second locally.
