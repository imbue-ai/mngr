Fixed a bug where muted agents could appear mixed in with the other rows
(typically alongside "PRs not loaded") whenever provider discovery transiently
failed during a refresh -- e.g. a flaky network connection to a remote
provider. Previously the board loaded the muted set with a separate
all-or-nothing discovery pass, so if any one provider failed to load, the
entire muted set came back empty and every agent was reclassified by its PR
state.

The muted flag is now surfaced as a regular agent field via kanpan's
`agent_field_generators` (online) and `offline_agent_field_generators`
(offline) hooks, so it rides on the same agent list the board already fetches
through `list_agents` -- which tolerates a failing provider. A provider failing
during a refresh no longer drops the muted classification of agents on the
providers that did load, and the muted bit is preserved for offline/unreachable
agents too.
