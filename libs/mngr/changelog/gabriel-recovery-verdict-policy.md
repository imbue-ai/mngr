Diagnostics for docker discovery reporting existing hosts/agents as absent (observed live: `mngr start <agent>` failed with "Agent not found" during a minds app launch, against a stopped host whose records another process read successfully one second later):

- `DockerVolume.listdir` now distinguishes a genuinely-missing directory (still `FileNotFoundError`, the normal fresh-env case) from any other `ls` failure inside the state container, which now raises `OSError` carrying the exit code and output.

- The docker host store logs a warning (instead of silently returning an empty list) when listing host records or a host's persisted agent data fails for any reason other than the directory not existing -- an empty result there makes hosts/agents invisible to discovery.

- Docker discovery warns when it reads zero host records while labeled host containers exist (the signature of an unreadable record store, not an empty environment).

- An agent lookup that is about to fail with "No agent(s) found matching" first logs a warning summarizing what discovery did return (per host: provider, name, id, state, and agent count), so a not-found caused by a discovery gap is distinguishable from a truly-absent agent after the fact.
