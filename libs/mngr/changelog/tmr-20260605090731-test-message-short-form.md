Fixed the `test_message_short_form` e2e tutorial test. It previously timed out
under the default 10s pytest timeout while creating the local command agent, and
it carried an incorrect `@pytest.mark.modal` marker even though messaging a
single named local agent never invokes Modal (the resource guard fails such
tests). Added `@pytest.mark.timeout(180)` and removed the `modal` marker. Also
strengthened the test to assert the message was actually delivered ("Message
sent to: my-task" / "Successfully sent message to 1 agent(s)") rather than only
checking the exit code. Test-only change; no user-facing behavior change.
