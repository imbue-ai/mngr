Adapted to mngr's new per-host agent-id model (the same agent id may exist on multiple hosts, e.g. while a workspace migrates between machines): the desktop client consumes the instance-keyed discovery aggregator API, tolerates both bare-id and instance-keyed `resolver_snapshot` envelope keys from the forward plugin, and logs a warning when a workspace agent id resolves to multiple machines instead of silently first-matching.

Workspace-level policy for duplicates (routing to the ACTIVE record's machine) is deliberately deferred to the migration work; see specs/allow-duplicate-agent-ids.md.
