Scheduled triggers no longer fail verification because of a provider the container cannot reach.

A Modal container has no docker daemon, so a docker provider there is unreachable for the container's whole life. `mngr list` inside it completes the listing but exits with the provider-inaccessible code, which the verification poll could not tell apart from a real failure, so it timed out while the agent it was watching ran fine. The container's mngr no longer installs the docker plugin at all, so there is no docker backend to register, probe, or fail on.

The `test_schedule_add` release tests also asked their triggers to create an agent of type `echo`, which is not a registered type, so every triggered `mngr create` failed to parse its arguments. They now ask for a `command` agent running `echo`.
