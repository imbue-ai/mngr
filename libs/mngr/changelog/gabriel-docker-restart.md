Fixed a data-loss bug in volume garbage collection. When a provider backend
became briefly unavailable during a `mngr` operation that runs GC (e.g. a Docker
daemon restart), `discover_hosts` could return an empty host list while
`list_volumes` still reported volumes on disk. `gc_volumes` then treated every
volume as orphaned and deleted it -- wiping the `host_dir` data of still-live
hosts (their host records survived, but their per-host volume directories did
not, so the containers could no longer be restarted).

`gc_volumes` now refuses to delete anything when it is handed an empty host list
but the provider still reports existing volumes, treating that combination as a
failed/partial discovery rather than "everything is orphaned". Genuine cleanup
is unaffected: destroying a host removes its volume together with its record, so
volumes never legitimately outlive a completely empty host list.
