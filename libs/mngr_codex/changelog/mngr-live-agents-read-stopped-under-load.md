Codex agents now read their process presence from the host's shared lifecycle snapshot, so a listing reads tmux and `ps` once per host rather than once per agent. When that snapshot can't be read, a codex agent reads UNKNOWN rather than STOPPED.

A laptop-side `mngr list` of a remote host (modal, vps, imbue_cloud) no longer reads every live codex agent as DONE. That listing knew only claude's process name and looked for the first word of codex's launch command, `(`, which never runs. The process name `codex` is now declared on the agent class, where that listing reads it.
