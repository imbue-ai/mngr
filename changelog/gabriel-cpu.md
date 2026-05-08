Fix CPU pin in minds-workspace-server caused by an inotify feedback loop on
`runtime/applications.toml`. The `_ApplicationsFileHandler` watcher reacted
to every event type that watchdog dispatches -- including `IN_OPEN` /
`IN_CLOSE_NOWRITE`, which were re-emitted by the handler's own re-read of
the file, pinning one CPU core per active agent. The handler now only
subscribes to mutation events (`on_modified`, `on_created`, `on_deleted`,
`on_moved`, `on_closed`) so a read no longer feeds the watcher.
