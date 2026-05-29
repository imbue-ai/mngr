Fixed the `test_list_short_form` e2e tutorial test (`mngr ls`): removed the
superfluous `@pytest.mark.modal` mark. In a fresh, agent-free environment
`mngr ls` only touches Modal via in-subprocess SDK gRPC (which the resource
guard cannot observe -- it only tracks the `modal` CLI binary in subprocesses)
and degrades gracefully when a provider is unreachable, so the test does not
depend on Modal and the mark tripped the guard's "never invoked" check. Also
strengthened the test to assert that the short form actually performs a listing
("No agents found") rather than only checking the exit code.
