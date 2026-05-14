Fix `test_create_headless` e2e release test: add `@pytest.mark.timeout(120)`
so pytest's 10s default does not trip while `mngr list` queries Modal, and
tighten the post-create assertion to verify the agent appears in `WAITING`
or `RUNNING` state on the local provider at `localhost`.
