`mngr list` now reports a leased machine's real recorded size. The `host.resource` fields (memory and disk) are read from the connector's current-size columns (`memory_units`, 1 unit = 1GiB, and `disk_gb`), which every start restamps, instead of the bake-time lease attributes, which are never updated after a `mngr imbue_cloud machines resize` and so reported the original 8GB after a resize.

- Stopped machines now report their size too (previously `host.resource` was empty for a non-running machine), so a client can show the size of a machine that is not currently running. The size is likewise reported for a running machine whose box could not be reached during the listing.

- The vCPU count still comes from the lease attributes; the client cannot derive it (it is the machine's proportional share of its box's threads).

- The parallel-discovery unit test now proves concurrency with a barrier every host must reach at once instead of bounding elapsed wall-clock time, which flaked on loaded CI sandboxes. The barrier's timeout sits below the package's 10s pytest-timeout so a regression fails with `BrokenBarrierError` rather than a pytest timeout.
