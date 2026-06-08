Fixed the `test_list_limit` e2e tutorial test: removed the superfluous
`@pytest.mark.modal` mark. `mngr list --limit 10` in a fresh environment never
invokes the Modal CLI (the only Modal usage the e2e resource guard can observe
in a subprocess), so the mark tripped the guard's `NEVER_INVOKED` check.

Strengthened the same test so it actually exercises `--limit`: it now creates
two agents, asserts that `mngr list --limit 10` shows both, and that
`mngr list --limit 1` truncates the result to a single agent (previously it
only ran the command against an empty environment and checked the exit code).
