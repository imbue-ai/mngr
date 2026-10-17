Updated a watcher test's fake process to pass a `ShutdownEvent`, which `RunningProcess` now requires in place of a plain `threading.Event`. (Affects test infrastructure only.)
