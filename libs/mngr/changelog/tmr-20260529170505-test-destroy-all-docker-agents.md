Fixed the "destroy all docker agents" tutorial command to include the `-` stdin
placeholder (`mngr list ... --ids | mngr destroy -f -`). Without `-`, `mngr destroy`
ignores piped stdin and errors out with "Must specify at least one agent".

Strengthened the corresponding e2e test (`test_destroy_all_docker_agents`) to
create a real Docker agent, confirm it is listed, run the destroy-all command,
and then assert the agent is actually gone -- previously it only ran the command
against an empty environment.
