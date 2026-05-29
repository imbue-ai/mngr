Fixed the `test_create_reuse_and_message` e2e tutorial test so it no longer
requires a default `commands.create.type` to be configured: the create command
now pins `--type command` (a sleep agent), matching the pattern used by the
other scripting/messaging tutorial tests.

Strengthened the same test to actually exercise the `--reuse` idempotency
contract: it now runs the create command twice and verifies the second run
reuses the existing agent (reported on stderr, same agent id, no duplicate in
`mngr list`) instead of provisioning a new one.
