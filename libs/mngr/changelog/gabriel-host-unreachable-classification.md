Split the overloaded `HostState.UNAUTHENTICATED` into two host states so consumers can distinguish two conditions that need opposite handling:

`UNAUTHENTICATED` now means our access credential was rejected at the host's access boundary (e.g. imbue_cloud's outer SSH refusing this machine's key): observation of the workspace is impossible and a restart routes through the same rejected key, so it is terminal rather than restart-worthy.

The new `UNREACHABLE` means the host was observed up but its inner sshd is not answering (a running container whose inner sshd died, or an inner-SSH connection error), which a host restart can revive. This is the condition the generic, docker, and imbue_cloud providers previously reported as `UNAUTHENTICATED`.

Both are listed by default (`mngr list --active` does not hide them) and never garbage-collected. Resolves the provider-vocabulary deferral flagged in PR #2247.
