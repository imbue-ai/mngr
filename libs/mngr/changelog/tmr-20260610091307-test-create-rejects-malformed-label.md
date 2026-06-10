Made the `test_create_rejects_malformed_label` e2e tutorial test robust to unreachable remote providers.

- The test's "no agent was left behind" verification ran a full `mngr list --format json`, which
  fans out to every enabled provider (docker, modal, ...). When the docker daemon was unavailable,
  the listing exited non-zero on a provider-discovery error unrelated to the malformed-label
  behavior under test, failing the assertion. Since `mngr create` defaults to the local provider,
  a leaked agent could only appear there, so the verification now scopes to `mngr list --provider
  local --format json`. This still asserts the real behavior while no longer depending on the
  health of remote providers.
