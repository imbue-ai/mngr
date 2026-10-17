`mngr list` now reports an opencode agent blocked on a tool-approval prompt as `WAITING` rather than `RUNNING`.

The plugin already treated such an agent as waiting, but only for the reader `mngr wait`/`message`/`find` use; the agent listing reads the lifecycle probe directly and never saw it, so `mngr list` showed `RUNNING` for an agent that was making no progress.

The plugin now answers `is_blocked_on_dialog()` and the probe folds that into the state it builds, so both readers agree.
