The Python programs Imbue Studio runs inside a workspace for backups (the backup-service check, the busy-chat gate probe, the backup-service update, and the in-place restore) are now real modules under `imbue/minds/workspace_programs/` instead of string literals, so ruff, ty and the ratchets check them like the rest of the app. Behavior is unchanged.

- Each program ships as a self-contained zip application built from the package's own source files: the program's module as `__main__.py`, plus the package modules it imports. `mngr exec` decodes it to a temporary file, runs it with the workspace's `python3`, and removes the file. The archive is deterministic and runs standalone from any file name, so a program can also be copied into a workspace and run later (for example by cron).

- Constants the app and a program share (the answer markers, the `official` remote) are defined once in the package and imported by both, replacing the duplicated values a unit test used to keep in step.

- A new project ratchet fails if a module in `workspace_programs/` imports anything outside the standard library and its own package. The bare-print and direct-subprocess ratchets exempt only that package, and the `time.sleep` count rises by one for the programs' shared `poll_until` helper, which replaces the three polling loops the scripts carried.

- `running_workspace` (in `apps/minds/conftest.py`) and `run_workspace_program_in_container` (in `imbue/minds/testing.py`) run a program inside the minds-snapshot sandbox's real default-workspace-template container; `test_workspace_programs_e2e.py` uses them to run the gate probe there.
