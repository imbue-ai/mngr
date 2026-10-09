Each staleness sweep now evicts the cached transcript scans of agents it no longer evaluates, keeping mngr's shared compaction transcript cache bounded to the agents still being checked.
