- Strengthened the `test_create_modal_no_connect_message` e2e/release test: in
  addition to checking that `mngr create --message` logs "Sending initial
  message", it now confirms the agent-side effect by polling `mngr transcript
  my-task --role user` until the submitted prompt appears in the agent's
  transcript. This verifies the message was actually received by the remote
  Claude agent, not just that the create command attempted to send it.
