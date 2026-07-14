Add the blueprint for named dockview layouts in the default workspace template (`blueprint/dockview-named-layouts/`).

The implementation itself lands in the `default-workspace-template` repo (same branch name there): named `desktop`/`mobile` layouts with per-client selection, "+"-menu save/load/delete dialogs, live cross-client sync, a client-activity event log, and layout-targeted `layout.py` ops with new `context` and `load` subcommands.
