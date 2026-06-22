- Fixed the headless-create e2e release test (`test_create_headless_no_connect_message`)
  so its post-create listing scopes to the local provider (`mngr list --provider
  local`). The agent is created on the local provider, so querying every enabled
  provider made the check fail whenever an unrelated remote provider (e.g. an
  enabled-but-unconfigured AWS provider) was unreachable.

- Removed the stale `@pytest.mark.rsync` mark from the same test: a local
  `command` agent runs its process in a tmux session but never syncs files over
  rsync, so the mark tripped the resource guard.

- Strengthened the test to confirm the headless `--no-connect` agent's host is
  actually live and reachable via `mngr exec my-task pwd`, not merely that the
  agent name appears in the listing.
