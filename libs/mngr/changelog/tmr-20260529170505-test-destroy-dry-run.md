Fixed the `mngr destroy` tutorial: the "dry-run to see what would be destroyed"
block referenced `mngr destroy - --dry-run`, but the `--dry-run` flag was removed
from multi-target commands (the recommended pattern is now composing with
`mngr list ... | mngr <subcommand> -`). The block now demonstrates previewing
what would be destroyed by listing the agents first (`mngr list --ids`).

Also fixed the corresponding e2e tutorial test (`test_destroy_dry_run`): added a
test timeout so it no longer hits the global 10s default while creating an agent,
and removed the inaccurate `@pytest.mark.modal` mark (the test creates a local
agent and only lists, so it never exercises Modal).
