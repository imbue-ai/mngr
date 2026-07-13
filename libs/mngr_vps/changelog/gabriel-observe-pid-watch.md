Agent listings from this provider now populate `AgentDetails.main_pid` (the agent's main process PID in the remote host's PID namespace), extracted from the same already-collected tmux/ps probe data.

Host listings set the new required `HostDetails.is_local` field to `False` (hosts from this provider are always remote).
