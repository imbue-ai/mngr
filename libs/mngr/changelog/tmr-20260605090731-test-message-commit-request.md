Fixed the `test_message_commit_request` git tutorial e2e test (`mngr msg`): it
now has a longer per-test timeout to accommodate cross-provider agent discovery,
and no longer carries a superfluous `@pytest.mark.modal` mark (the command only
contacts Modal via the mngr subprocess SDK, which the resource guard cannot
attribute to the test, so the mark always tripped the "never invoked" check).
The test also asserts that `mngr msg` actually reports delivery to the target
agent, not just that the command exits cleanly.
