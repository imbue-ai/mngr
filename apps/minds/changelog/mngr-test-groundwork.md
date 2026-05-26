# Fix stale `LaunchMode.LOCAL` reference in agent_creator_test.py

`apps/minds/imbue/minds/desktop_client/agent_creator_test.py:130` was
still referencing `LaunchMode.LOCAL`, which was renamed to
`LaunchMode.DOCKER` in an earlier PR (commit 609e7d46b). The rename
caught every `.LOCAL` usage that existed at the time, but a sibling
branch added a new `.LOCAL` reference in this test that wasn't caught
when it landed via merge. That broke `test_no_type_errors` across every
project in the monorepo (each runs the type checker on its dependency
graph, which includes `apps/minds`). Renamed the stray usage to
`LaunchMode.DOCKER` to match the rest of the file.
