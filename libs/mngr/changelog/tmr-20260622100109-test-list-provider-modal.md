Strengthened the `test_list_provider_modal` e2e tutorial test (covering `mngr list --provider modal`).

The test now also proves the `--provider` flag genuinely discriminates rather than just printing every agent: after confirming the modal-hosted agent appears under `mngr list --provider modal`, it runs `mngr list --provider local` and asserts the modal agent is absent. This is a cheap, local-only listing (no extra Modal provisioning) that verifies a non-matching provider query excludes the agent.
