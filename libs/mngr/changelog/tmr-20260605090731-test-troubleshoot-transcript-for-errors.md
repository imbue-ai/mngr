Fixed the `test_troubleshoot_transcript_for_errors` e2e tutorial test. The
troubleshooting block substitutes a lightweight `command` agent for the
tutorial's claude agent, and `mngr transcript` correctly rejects command
agents (they produce no common transcript). The test now asserts that clear
diagnostic instead of expecting success, adds a timeout override to cover the
agent-create latency, and drops the superfluous `modal` mark (the transcript
diagnostic is a local, client-side agent-type check that never scans modal).
