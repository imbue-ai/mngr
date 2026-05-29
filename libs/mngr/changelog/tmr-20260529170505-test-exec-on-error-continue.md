Fixed the EXEC tutorial block for multi-agent error handling: `mngr exec` has
no `-a`/`--all` flag, so the example now uses the documented pipe pattern
`mngr list --ids | mngr exec - --on-error continue "..."`. Added a per-test
timeout to the corresponding e2e tutorial test so it is not killed by the
global 10s pytest timeout while creating an agent, and dropped the superfluous
`@pytest.mark.modal` mark (the test creates a local command agent and never
invokes Modal). Strengthened the happy-path assertion to verify the command
actually ran on the agent, and added an unhappy-path test that verifies
`--on-error continue` attempts every agent and surfaces a non-zero exit code
when the command fails on all of them.
