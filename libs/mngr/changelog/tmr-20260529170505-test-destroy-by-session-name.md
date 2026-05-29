Removed a superfluous `@pytest.mark.modal` from the `mngr destroy --session`
release test (`test_destroy_by_session_name`): the tutorial's literal session
name fails input validation before any provider code runs, so the modal mark
was flagged by the resource guard. Also tightened the test's assertion to check
for the specific "does not match the expected format" validation error instead
of accepting any non-zero exit code.
