# Model discovery's provider outcomes as one map instead of parallel lists

`DiscoveryOutcome` now carries a single `results_by_provider` map -- each provider's name to the instance that answered, or to an `Unreachable` recording why it could not be reached -- replacing the parallel `providers` / `skipped_providers` lists and the `is_empty` flag that distinguished a reached-but-empty provider from an unreachable one. "Unavailable providers" is now a filter over that map rather than a boolean predicate, and a provider that answered but holds nothing simply contributes no hosts, so it needs no case of its own. `providers` and `unavailable_providers` remain as derived views, so `mngr list` and the agent-lookup error path (which names an unreachable backend instead of reporting the agent as missing) are unchanged. No user-visible behavior change.

(This branch began as a crispy-comments pass over PR #304; the `DiscoveryOutcome` docstring trimmed there is rewritten by this remodel.)
