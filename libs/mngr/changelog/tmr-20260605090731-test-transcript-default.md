Fixed the `mngr transcript` e2e tutorial tests (`test_transcript.py`). They
previously created a `command`-type agent, which `mngr transcript` rejects
(command agents produce no common transcript), so the tests could never pass.
They now create a real local `claude` agent with an initial message, wait for
the first assistant reply, and assert on the actual transcript content for each
variant (`--role`, `--tail`, `--format jsonl`).
