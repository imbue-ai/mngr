Fixed the "destroy all Modal agents" tutorial example. The piped form now
correctly passes `-` to `mngr destroy` so it reads agent IDs from stdin
(`mngr list --include 'host.provider == "modal"' --ids | mngr destroy - -f`);
previously it omitted `-` and errored with "Must specify at least one agent".

Strengthened the corresponding e2e release test (`test_destroy_all_modal_agents`)
to create a real Modal agent, confirm it is listed, destroy it via the
filter+stdin pipeline, and verify it no longer appears among active agents.
