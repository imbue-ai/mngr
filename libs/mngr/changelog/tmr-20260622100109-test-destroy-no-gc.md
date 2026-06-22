Fixed the `test_destroy_no_gc` release e2e test so it passes deterministically.

The test created a localhost command agent and verified its removal with a plain `mngr list`, which enumerates every enabled provider and exits non-zero if any remote provider (e.g. Docker) is unreachable -- something the e2e environment does not guarantee. The verification listing is now scoped to `mngr list --provider local` (the agent's provider), matching the existing pattern in `e2e/test_errors.py`.

The test also carried `@pytest.mark.rsync`, but a localhost command agent is set up via a git worktree and never invokes rsync, so the resource guard failed the otherwise-passing test ("marked rsync but never invoked rsync"). Removed the spurious mark.
