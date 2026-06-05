Fixed the `mngr exec` "error handling on multiple agents" tutorial block, which
still referenced the removed `-a`/`--all` flag. It now demonstrates the supported
pattern for targeting all agents: `mngr list --ids | mngr exec - ...`. The
corresponding e2e release test was updated to match and to verify the command
actually runs on the agent's host.
