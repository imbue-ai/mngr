Fixed a bug where muted agents could appear mixed in with the other rows
(typically alongside "PRs not loaded") after the machine woke from sleep.
The board's muted set was loaded with a single all-or-nothing discovery call,
so if any one provider failed to load during a refresh -- e.g. a remote
provider that was momentarily unreachable right after waking -- the entire
muted set came back empty and every agent was reclassified by its PR state.
Muted state is now discovered per-provider (in parallel), so one provider's
failure no longer wipes out the muted classification of agents on the
providers that did load. This mirrors how the board's agent list already
tolerates per-provider failures.
