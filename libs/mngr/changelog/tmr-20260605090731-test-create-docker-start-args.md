Fixed the `test_create_docker_start_args` e2e tutorial test: the `mngr create`
invocation now passes `--type command -- sleep ...` so that the isolated test
environment (which has no default agent type configured) can create the agent
and keep the container alive for the follow-up `mngr exec my-task hostname`
assertion that verifies the `-s "--hostname=..."` start arg was forwarded to
`docker run`.
