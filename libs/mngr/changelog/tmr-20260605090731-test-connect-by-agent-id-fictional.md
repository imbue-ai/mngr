Removed a superfluous `@pytest.mark.modal` mark from the
`test_connect_by_agent_id_fictional` e2e test. The test connects to a
non-existent agent id and only verifies that mngr parses the id-as-target
syntax and reports a clean "not found" error; it never shells out to the
`modal` CLI (the only Modal chokepoint tracked inside the mngr subprocess),
so the resource guard correctly flagged the mark as never invoked. The test
still runs in the release suite (it is `@pytest.mark.release`), which executes
on Modal infrastructure regardless of the mark. Also strengthened the test's
assertion to confirm the command exits non-zero and the error explicitly names
the missing agent id. No production behavior change.
