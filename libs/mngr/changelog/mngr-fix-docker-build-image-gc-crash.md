Fixed `mngr destroy` (and `mngr gc`) crashing with a Docker `409 Conflict` traceback when a Docker host's per-host build image is still referenced by a (often orphaned, stopped) container.

The post-destroy garbage collection removed the build image without `force`, which Docker refuses with "must be forced - container is using its referenced image", and the error was not caught in `delete_host`, so it aborted GC and surfaced as a failed `mngr destroy` even though the requested agent had already been destroyed.

The build image is now force-removed, and a removal failure during GC is logged and skipped instead of aborting the whole command.
