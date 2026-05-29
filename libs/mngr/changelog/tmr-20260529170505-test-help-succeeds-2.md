Fixed the `test_help_succeeds` e2e tutorial test: it incorrectly asserted that
`mngr --help` lists `push` and `pull` as top-level commands. Those operations are
performed via `mngr git push`/`mngr git pull` (or `rsync`), so the test now asserts
on the `git` command instead.
