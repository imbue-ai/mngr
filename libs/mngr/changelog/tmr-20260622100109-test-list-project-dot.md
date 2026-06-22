Hardened the PROJECTS tutorial e2e coverage for `mngr list --project .`:

- Fixed `test_list_project_dot` so it no longer hangs on the default 10s pytest timeout and no longer aborts when a backend plugin installed in the dev monorepo (Docker, or the AWS/cloud backends) is unreachable. The listing is now pinned to the local provider, which still exercises the `.` -> current-project expansion (it is resolved before any provider is queried).

- Added `test_list_project_dot_matches_current_project`, which seeds a local agent that inherits the current project and asserts that `mngr list --project .` matches it while an unrelated project name does not -- proving `.` resolves to the live current project rather than being merely accepted.
