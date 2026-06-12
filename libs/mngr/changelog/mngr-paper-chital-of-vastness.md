`mngr destroy` (and any garbage-collection run) no longer aborts when the Docker
provider cannot remove a per-host build image because a container still
references it. Previously `_remove_build_image` called `images.remove(tag)`
with no error handling, so Docker's `409 Conflict` ("must be forced - container
... is using its referenced image") propagated out of the GC worker thread and
crashed the whole command. This was reachable whenever a build image was still
referenced by a live container -- including the common case where byte-identical
default-Dockerfile builds deduplicate to a single image id shared across hosts.
The build-image untag is now best-effort: a removal conflict is logged as a
warning and skipped, matching how snapshot-image and host-volume cleanup in
`delete_host` already tolerate failures.
