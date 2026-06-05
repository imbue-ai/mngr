Fixed the host-diagnostics block in the mega tutorial. `mngr exec` takes the
command to run as a single argument (its last positional), so the previous
`mngr exec my-task -- ps aux` form parsed `ps` as a second agent name and
failed with "Agent not found". The tutorial now quotes each command, e.g.
`mngr exec my-task "ps aux"`, which also makes the `cat ... | tail -20` pipe
run on the agent's host as intended.
