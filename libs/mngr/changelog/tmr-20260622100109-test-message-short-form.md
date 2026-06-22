Fixed the `test_message_short_form` e2e tutorial test, which was marked
`@pytest.mark.rsync` even though `mngr msg` delivers messages over tmux and the
local command-agent setup never invokes rsync. The resource guard turned the
otherwise-passing test into a failure; removing the spurious mark fixes it.

Also tightened the test to assert that the no-op delivery path ("No agents found
to send message to") is not taken, mirroring the long-form `mngr message` test.
