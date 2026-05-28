Fixed a bug where muted agents could appear mixed in with the other rows
(typically alongside "PRs not loaded") whenever provider discovery
transiently failed during a refresh -- e.g. a flaky network connection to a
remote provider. The board's muted set was loaded with a single
all-or-nothing discovery call, so if any one provider failed to load, the
entire muted set came back empty and every agent was reclassified by its PR
state. Muted state is now discovered per-provider (in parallel), so one
provider's failure no longer wipes out the muted classification of agents on
the providers that did load. This mirrors how the board's agent list already
tolerates per-provider failures.
