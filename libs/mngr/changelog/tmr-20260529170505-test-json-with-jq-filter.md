Fixed the mega-tutorial's "combine json with jq" example. `mngr list --format json`
emits a `{"agents": [...], "errors": [...]}` object, not a bare array, so the
documented `jq '.[] | ...'` filter errored out. The example now uses
`jq '.agents[] | ...'`, which correctly iterates the agents.

Hardened the corresponding `test_json_with_jq_filter` e2e tutorial test: gave it a
60s timeout (the default 10s was too tight for the real `mngr list` invocation),
dropped the stale `@pytest.mark.modal` mark (the command never invokes the modal
binary, so the mark could never be satisfied), and added an assertion that the
filter produces empty output in a fresh, agent-less environment.
