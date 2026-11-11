Fixed: a host no longer idle-stops itself out from under an agent that is working.

An agent's activity file was refreshed only while a turn was in progress. An agent whose turn ended with work it started still running has no turn marker -- that is what `RUNNING_BACKGROUND` means -- so nothing refreshed the file, and the host's idle watcher saw an agent that had gone quiet. Every provider defaults to a 800-second idle timeout, so against a four-hour lease a host could stop itself about thirteen minutes into work that had hours left to run. The same stale file is what `mngr ls` reported as `idle_seconds`, so a listing showed an agent idling for hours beside a state saying it was working.

Work outstanding past a turn now counts as activity, so the host stays up for it and the idle time reads as zero. Only agents created from this version report it: the activity tracker and the work-lease library are both written into an agent's own area when the agent is made.
