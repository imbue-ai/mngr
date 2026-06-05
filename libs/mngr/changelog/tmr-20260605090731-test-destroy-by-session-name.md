Fixed the `test_destroy_by_session_name` e2e test, which was failing because it
was marked `@pytest.mark.modal` even though the command under test
(`mngr destroy --session my-session-name`) fails fast on input validation and
never provisions a modal-backed agent. Removed the spurious mark and added a
happy-path test that creates a real agent and destroys it via its derived tmux
session name, verifying the agent is actually gone.
