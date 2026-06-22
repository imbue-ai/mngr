Fixed the `test_archive_command` e2e tutorial test for the `mngr archive` command so it passes reliably:

- Added a 180s timeout (the default 10s was too short for the create + stop + archive + list round-trip).

- Scoped the verification listing to `--provider local` so it no longer enumerates remote providers (AWS discovery fails hard without credentials), matching the sibling `test_stop_archive`.

- Removed the inapplicable `@pytest.mark.rsync` and `@pytest.mark.modal` marks: the test exercises a local git-worktree command agent, which uses neither rsync nor Modal.

- Added an assertion that the archived agent stays `STOPPED`, confirming archive preserves state rather than tearing the agent down.
