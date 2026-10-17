- Marked `test_exec_echo_on_modal` with `@pytest.mark.flaky` (like its sibling
  Modal exec tests): fresh Modal sandboxes transiently accept TCP before sshd
  answers the SSH handshake, so a slow Modal window can outlast mngr's bounded
  banner retry. Offload now retries the whole test.
