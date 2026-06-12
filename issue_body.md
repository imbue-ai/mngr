## Summary

`mngr destroy` (and any GC run) aborts with an uncaught `docker.errors.APIError: 409 Conflict` when the Docker provider tries to remove a per-host build image that a container still references. Build-image cleanup is a best-effort step, but the conflict is not caught, so it propagates out of the GC worker thread and crashes the whole command.

```
APIError: 409 Client Error for http+docker://localhost/v1.52/images/mngr-build-host-56674c4fe6a747c383b804267b8af99e?force=False&noprune=False: Conflict ("conflict: unable to delete mngr-build-host-56674c4fe6a747c383b804267b8af99e:latest (must be forced) - container b6c97bd74492 is using its referenced image b7dd5dc9d534")
```

## Root cause

`DockerProviderInstance._remove_build_image` removes the per-host build tag with no `force` and, deliberately, no exception handling:

`libs/mngr/imbue/mngr/providers/docker/instance.py:780`
```python
def _remove_build_image(self, host_id: HostId) -> None:
    ...
    tag = self._build_image_tag(host_id)
    if not self._docker_client.images.list(name=tag):
        logger.trace("No build image to remove for host {}", host_id)
        return
    self._docker_client.images.remove(tag)   # line 795 -- no force=, no try/except
```

The docstring states this is intentional: *"any removal failure propagates so it is visible rather than silently leaking the image."* That assumption only holds when the sole failure mode is a genuine leak. It does not hold for the **"image still in use"** case, where the image is *not* leaked (a live container still references it) and aborting is the wrong response.

Docker returns **HTTP 409** from `DELETE /images/<tag>` when **both** of these are true:
1. the tag being removed is the **last** tag on its image, and
2. a container (running *or* merely created) still references that image.

Three facts combine to make this reachable from the GC path:

1. **`delete_host` never removes the container.** `delete_host` (the GC deletion path) removes the host record, snapshot images, and the volume dir, then calls `_remove_build_image` as a *"Defensive untag in case destroy_host did not run."* It assumes `destroy_host` already removed the container, but it never removes one itself:

   `libs/mngr/imbue/mngr/providers/docker/instance.py:1472`
   ```python
   def delete_host(self, host: HostInterface) -> None:
       ...
       for snap in host_record.certified_host_data.snapshots:
           try:
               self._docker_client.images.remove(snap.id)
           except docker.errors.DockerException as e:           # snapshot removal: tolerant
               logger.warning("Error removing snapshot image {}: {}", snap.id, e)
       ...
       try:
           self._state_volume.remove_directory(f"volumes/{volume_id}")
       except (FileNotFoundError, OSError, MngrError) as e:      # volume removal: tolerant
           logger.trace("No host volume to clean up for {}: {}", host_id, e)
       # Defensive untag in case destroy_host did not run (idempotent).
       self._remove_build_image(host_id)                        # build-image removal: NOT tolerant
   ```
   So when `delete_host` runs while the container still exists (host stopped/crashed/failed but not destroyed, an interrupted `mngr destroy`, or a prior `destroy_host` whose `container.remove(force=True)` failed and only logged a warning at `instance.py:1460`), that container still pins the build image.

2. **Per-host build images deduplicate.** Build tags are per-host (`mngr-build-{host_id}`, `instance.py:776`), but a byte-identical build (the default-Dockerfile case, `create_host` -> `_build_default_image`, `instance.py:1120-1123`) yields a **single image id with many tags**. A container belonging to one host can therefore pin the very image that *another* host's build tag points to. This is consistent with the error: image `b7dd5dc9d534` had a single remaining tag (`mngr-build-host-56674...`) while container `b6c97bd74492` referenced that image.

3. **The conflict is never caught on the way up.** `_remove_build_image` raises `docker.errors.APIError`, which is **not** an `MngrError`. The GC worker only catches `MngrError`:

   `libs/mngr/imbue/mngr/api/gc.py:335` (raises) -> `gc.py:427` (`except MngrError`, does not match) -> re-raised at `gc.py:293` (`future.result()`) -> `gc()` (`gc.py:89`) -> `_run_post_destroy_gc` (`destroy.py:759`) -> `destroy` (`destroy.py:313`) -> CLI crash.

So a best-effort untag of one host's image aborts GC for **all** hosts and fails the user's `mngr destroy`.

## Reproduction

A merely *created* (not even running) container reproduces it. Run against a local Docker daemon:

```python
import io, docker
client = docker.from_env()
tag = "mngr-build-host-repro409test"

image, _ = client.images.build(fileobj=io.BytesIO(b"FROM busybox:latest\nCMD sleep 600\n"), tag=tag, rm=True)
container = client.containers.create(tag, name="mngr-build-host-repro409test-c")  # holds a reference

client.images.remove(tag)   # exactly what _remove_build_image does -> raises 409
```

Observed output (structurally identical to the report -- same API `v1.52`, same `force=False&noprune=False`, same *"must be forced - container ... is using its referenced image ..."*):

```
docker.errors.APIError: 409 Client Error for http+docker://localhost/v1.52/images/mngr-build-host-repro409test?force=False&noprune=False: Conflict ("conflict: unable to delete mngr-build-host-repro409test:latest (must be forced) - container 385ab00efba9 is using its referenced image 770682efe933")
```

Wrapping the call in `except docker.errors.DockerException` makes it a non-fatal warning and GC continues -- confirming both the cause and the fix.

## Suggested fix

Make `_remove_build_image` tolerate the conflict, mirroring the snapshot/volume cleanup that already wraps `images.remove(...)` in `delete_host`. Do **not** pass `force=True` -- that would delete an image still referenced by another host's live container.

```diff
--- a/libs/mngr/imbue/mngr/providers/docker/instance.py
+++ b/libs/mngr/imbue/mngr/providers/docker/instance.py
@@ def _remove_build_image(self, host_id: HostId) -> None:
         tag = self._build_image_tag(host_id)
         if not self._docker_client.images.list(name=tag):
             logger.trace("No build image to remove for host {}", host_id)
             return
-        self._docker_client.images.remove(tag)
+        try:
+            self._docker_client.images.remove(tag)
+        except docker.errors.DockerException as e:
+            logger.warning("Could not remove build image {} (still in use?): {}", tag, e)
```

(Verified locally: with this change the reproduction above logs a warning instead of raising, and the GC run completes.) A regression test could assert that `delete_host`/`destroy_host` do not raise when `images.remove` raises a 409 `APIError`.

A complementary hardening would be to have `delete_host` remove the host's container before untagging (so the host's *own* container can never block its build-image cleanup), but the asymmetry in (3) -- build-image removal being the only intolerant cleanup step -- is the direct cause of the crash and is the minimal fix.

## Environment

- **mngr version:** 0.2.12
- **Commit hash inspected:** `4e4d4d713013da745047b5bbcc1a56182a1d5519`
- **Docker Engine:** 29.1.2 (Docker Desktop), API version `1.52`
- **docker-py (Python SDK):** 7.1.0
- **Python:** 3.12.12
- **OS:** macOS (Darwin 25.4.0, arm64)
