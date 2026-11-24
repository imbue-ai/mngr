`mngr upgrade` upgrades agents on remote hosts too, reaching them over SSH as `mngr start` does, and never starts a host. A line, and the `mngr start` or `mngr create` command it gives, names a remote agent by its address (`NAME@HOST.PROVIDER`); every JSON entry gives the agent's `host` (`id`, `name`, `provider_name`).

- An agent is skipped with kind `NOT_IDLE` when its host is not running, cannot be looked up, or loses its connection while taking the host lock, reading the agent's inputs, staging its new files, checking for a dialog or checking that it can start, all before the upgrade changes the agent; the detail names the host's state, with the `mngr start` command for a stopped, paused or crashed host, or the error that kept it from being reached. A connection lost while the agent's tmux panes and work state are read skips it because its state could not be read.

- A connection lost at an agent's stop, swap or start fails it at that step; a re-run gives it its files, and an agent left stopped needs `mngr start`. A host lock lost by the end of a real upgrade fails it at a new `unlock` step.

- Once a host's connection is lost at any of these steps, its later agents are skipped without reaching it again; after a connection lost while an agent's tmux panes and work state are read, the next agent tries the host again.

- Any other host error before the upgrade changes an agent, such as a remote host's lock command failing or its lock being taken by another mngr while the agent's state was read, fails the agent at the `lock` step, untouched, and the run goes on.

- Each host is looked up once and its named agents loaded in one read; an agent's inputs are read in one host command, and an agent found current costs five host operations.

- The agent running the command is matched by its id on the local host only, and a multi-line error stays on its agent's line.
