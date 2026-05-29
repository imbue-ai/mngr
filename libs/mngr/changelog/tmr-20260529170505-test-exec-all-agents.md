Fixed the "run a command on all agents" tutorial example. `mngr exec` has no
`-a`/`--all` flag, so `mngr exec -a "whoami"` failed with "No such option: -a".
The tutorial now uses the documented idiom `mngr list --ids | mngr exec - "whoami"`,
matching how the same operation is shown elsewhere in the tutorial and in the
`mngr exec` help examples. The corresponding e2e test (`test_exec_all_agents`)
was updated to run the corrected command and to verify it actually executes on
the targeted agent.
