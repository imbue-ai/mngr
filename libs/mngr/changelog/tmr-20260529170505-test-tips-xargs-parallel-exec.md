Fixed the `test_tips_xargs_parallel_exec` e2e tutorial test, which covers the
`mngr list ... --ids | xargs -P 5 -I {} mngr exec {} ...` parallel fan-out tip.
The test previously ran the pipeline against an empty agent list, so it never
invoked modal and failed the `@pytest.mark.modal` resource guard. It now creates
two real modal command agents, runs the pipeline against them, and verifies each
host echoed its own `MNGR_AGENT_ID` and printed an absolute working directory.
