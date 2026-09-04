Hardened the agent start path against wedges and mistaken kills (mngr-internal#807):

- The per-agent launch batch (`tmux new-session` and friends) is now bounded at 90 seconds per attempt (over SSH the batch is retried like any other idempotent command when the read times out, so the worst case is a few minutes, still inside the 10-minute lock wait below). A wedged tmux server or client used to hang `mngr start` forever, and since the start runs under the host lock, everything queued behind it too; it now fails with an `AgentStartError` naming the agent and the bound.

- The pre-launch "is this agent's tmux session already up?" probe no longer guesses: a probe that times out raises a `CommandTimeoutError` and aborts the start, so the stale-process reap that runs before a launch only ever fires on a definitive "no session" answer. Previously, on local hosts, a slow `tmux has-session` read as "no session" and the reap killed the live agent's whole process tree (for a minds services agent: its supervisord and every service under it).

- `mngr start` (and every other path that starts an agent through the shared locked helper) now waits at most 10 minutes for the host lock instead of forever, and a timeout raises a `LockNotHeldError` that names the host and the agents it was trying to start.

- Starting a stopped agent on the way to messaging, connecting to, exec'ing in, or capturing it now happens under the host lock, like `mngr start` itself. Two concurrent unlocked starts of the same agent could previously both see "no session", and the slower one's reap would kill the tree the faster one had just launched.

- A command run with `raise_on_timeout=True` on a remote (SSH) host now surfaces a timeout as `CommandTimeoutError`, like it already did locally, instead of folding it into the generic `HostConnectionError`. Without this the two bounds above only behaved as described on local hosts: over SSH a wedged launch surfaced as a connection error instead of the named `AgentStartError`, and a timed-out session probe surfaced as a connection error instead of the `CommandTimeoutError` that lets the start abort cleanly today.
