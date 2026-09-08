`mngr cleanup` now destroys hosts concurrently (up to 16 at a time) instead of one after
another. Destroying a host is almost entirely waiting on the provider, so a sweep of a large
backlog was bound by that wait: the daily TMR host cleanup was getting through roughly 90 of
its 1582 stale hosts per hour and hitting its three-hour timeout with the great majority of
them untouched.

Hosts are still reported in the order they were selected, so the destroyed-agent list and the
recorded failures do not depend on which host happened to finish first. `ErrorBehavior.ABORT`
keeps its previous meaning -- stop at the first failure, leaving every later host untouched --
which is only well defined when hosts are taken one at a time, so that behavior stays
sequential. The `mngr cleanup` command itself always continues past errors, so it always gets
the concurrent path.

`test_connect_cli_runs_custom_connect_command` and `test_stop_archive_sets_archived_at_label` are
now marked `@pytest.mark.flaky` with a 60s per-test timeout, matching the precedent on the sibling
tmux CLI tests. Both create a real tmux agent, run in about two seconds locally, and tripped the
global 10s pytest-timeout on a slow CI sandbox (incidental flakes hit on this branch, which touches
neither code path; not filed to Linear from this workspace -- the scheduled flake sweep will
reconcile them).
