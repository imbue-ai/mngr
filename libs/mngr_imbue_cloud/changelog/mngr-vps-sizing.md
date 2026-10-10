- The slice VPS clients (`QemuSliceVpsClient` and the test double) implement the new `VpsClientInterface.get_instance_shape` (unavailable, like the other cloud-ordering operations: a slice is carved, never described), so the `imbue_cloud_slice` backend and the leased-slice container rebuild still construct.

- A slice host's record now carries the shape the carve gave it (vCPUs, RAM, data disk), so `mngr list` through the slice backend reports that size instead of the former 1 CPU / 1 GB placeholder.
