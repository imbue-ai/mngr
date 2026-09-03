Marked all four `test_exec_*_on_modal` acceptance tests flaky again, so offload
retries them.

Each one creates a Modal agent, which deploys the `snapshot_and_shutdown`
function into the shared Modal app and so races concurrent deploys from the rest
of the suite. The deploy's own bounded retry rides out the usual case but can be
exhausted under load, surfacing as
`ModalProxyAppLockedError: ... Function fu-<id> not found`. The create and
snapshot tests that go through the same path were already marked.

This partially reverts MIND-202, which dropped the marker from these four after
fixing the fresh-sandbox SSH banner race at the source. That fix stands; the
race retried here is a different one -- the shared-app deploy lock -- and it is
a property of the agent-creation helper all four tests share, so the marker goes
back on all four rather than only the two seen failing.
