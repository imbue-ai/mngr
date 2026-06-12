## Summary

`mngr destroy` (and any command that triggers `gc_machines`) crashes with an uncaught
Docker `APIError: 409 Conflict` when garbage-collecting an aged-out **stopped / failed /
crashed** Docker host. The post-destroy GC calls `delete_host`, which tries to remove the
per-host build image while the host's container is still present and still references that
image. Docker refuses to delete the (single-tag) image without `force`, returns 409, and the
error is not an `MngrError`, so it propagates through the GC worker thread and aborts the whole
command.

```
docker.errors.APIError: 409 Client Error for
http+docker://localhost/v1.52/images/mngr-build-host-56674c4fe6a747c383b804267b8af99e?force=False&noprune=False:
Conflict ("conflict: unable to delete mngr-build-host-56674c4fe6a747c383b804267b8af99e:latest
(must be forced) - container b6c97bd74492 is using its referenced image b7dd5dc9d534")
```

## Environment

- **mngr version:** 0.2.12
- **Commit hash inspected:** `4e4d4d713013da745047b5bbcc1a56182a1d5519`
- **Docker Engine (server):** 29.1.2, API `v1.52` (matches the `/v1.52/images/...` URL in the error)
- **docker-py (SDK):** 7.1.0
- **requests:** 2.34.0
- **Python:** 3.12.12
- **OS:** macOS (Darwin 25.4.0, arm64)

> Note: the line numbers in the reported traceback (`delete_host` at `:1466`, `images.remove`
> at `:788`) are from the 0.2.12 release build. On the inspected commit the same code lives at
> `delete_host` `:1472` and `_remove_build_image` `:795`; the logic is identical.

## Root cause

The per-host build image (`mngr-build-<host_id>`, built in `create_host`) is a **freshly built
image with a single tag**. Removing its only tag while a container still references it requires
`force`; otherwise the daemon would have to delete the underlying image, which it refuses to do
while a container (even a *stopped* one) points at it.

`_remove_build_image` removes the tag **without `force`**:

`libs/mngr/imbue/mngr/providers/docker/instance.py:780`
```python
def _remove_build_image(self, host_id: HostId) -> None:
    ...
    tag = self._build_image_tag(host_id)
    if not self._docker_client.images.list(name=tag):
        logger.trace("No build image to remove for host {}", host_id)
        return
    self._docker_client.images.remove(tag)        # :795  -- no force=True
```

This is only safe if no container references the image. `destroy_host` guarantees that by
removing the container **first**:

`libs/mngr/imbue/mngr/providers/docker/instance.py:1440` (`destroy_host`)
```python
container = self._find_container_by_host_id(host_id)
if container is not None:
    try:
        container.remove(force=True)              # :1459 -- container removed first
    except docker.errors.DockerException as e:
        logger.warning("Error removing container: {}", e)
self._remove_build_image(host_id)                 # :1465 -- now safe
```

But `delete_host` — the path the crash comes from — **never removes the container** before
calling `_remove_build_image`:

`libs/mngr/imbue/mngr/providers/docker/instance.py:1472` (`delete_host`)
```python
host_record = self._host_store.read_host_record(host_id, use_cache=False)
if host_record is not None:
    for snap in host_record.certified_host_data.snapshots:
        try:
            self._docker_client.images.remove(snap.id)     # guarded
        except docker.errors.DockerException as e:
            logger.warning("Error removing snapshot image {}: {}", snap.id, e)
# ... volume removal (guarded) ...
self._remove_build_image(host_id)                          # :1497 -- UNguarded, no container removal
```

Note the asymmetry: the snapshot-image removal and the volume removal in `delete_host` are both
wrapped in `try/except docker.errors.DockerException`, but `_remove_build_image` is not — so a
Docker error here is fatal.

### Why a container is still present when `delete_host` runs

`delete_host` is invoked by GC for **offline** hosts, and a Docker host is "offline" whenever its
container is not *running* — the container may still exist:

- `get_host` only returns an online `Host` when the container is **running**
  (`instance.py:1531`); otherwise it returns an `OfflineHost` from the record.
- `_find_container_by_host_id` lists with `all=True` (`instance.py:929`), so stopped/exited
  containers are still found and still reference the build image.
- `stop_host` uses Docker's native stop and **keeps the container** (`instance.py:1259`), so a
  `STOPPED` host always has a lingering container + build image.
- A `FAILED`/`CRASHED` host (create failed after the container was started, or the container
  exited) also leaves the container behind, and `destroy_host` was never run.

`derive_offline_host_state` (`hosts/offline_host.py:245`) maps such hosts to
`STOPPED` / `FAILED` / `CRASHED`, all of which GC will permanently delete once aged:

`libs/mngr/imbue/mngr/api/gc.py:317` (`_gc_single_host`)
```python
if not isinstance(host, OnlineHostInterface):
    seconds_since_stopped = host.get_seconds_since_stopped()
    if (seconds_since_stopped is not None
            and seconds_since_stopped > provider.get_max_destroyed_host_persisted_seconds()):
        agent_refs = host.discover_agents()
        if len(agent_refs) == 0 or host.get_state() in (FAILED, CRASHED, DESTROYED):
            if not dry_run:
                provider.delete_host(host)        # :335 -- container still present -> 409
```

Only properly `DESTROYED` hosts are safe here, because `destroy_host` already removed both the
container and the build image (so `_remove_build_image` is a no-op). For every other offline
state, the build image is still tagged and a container still references it.

### Why it crashes the whole command

The 409 is a `docker.errors.APIError`, which is **not** an `MngrError`. `_gc_single_host` only
catches `MngrError` (`gc.py:427`), so the `APIError` escapes the worker thread, is stored in the
`Future`, and `gc_machines` re-raises it at `future.result()` (`gc.py:293`) — taking down the
entire `mngr destroy` invocation instead of failing just that one host.

## Minimal reproduction

Mirrors `_remove_build_image` exactly — a fresh single-tag image plus a stopped container that
references it (the state a stopped/failed mngr host is left in):

```python
import io, docker
client = docker.from_env()
TAG = "mngr-build-repro-409:latest"
image, _ = client.images.build(fileobj=io.BytesIO(b'FROM alpine:3.19\nCMD ["sleep","1"]\n'),
                               tag=TAG, rm=True)
container = client.containers.create(TAG, name="mngr-build-repro-409-ctr")  # stopped container
client.images.remove(TAG)   # exactly what _remove_build_image does (no force) -> raises 409
```

Output (Docker 29.1.2, docker-py 7.1.0) — identical shape to the report, same
`force=False&noprune=False`:

```
409 Client Error for http+docker://localhost/v1.52/images/mngr-build-repro-409:latest?force=False&noprune=False:
Conflict ("conflict: unable to delete mngr-build-repro-409:latest (must be forced) -
container a7857e4f49c4 is using its referenced image e16082ae21ef")
```

End-to-end this is hit by:
1. `mngr create` on the Docker provider (builds `mngr-build-<id>`, runs a container).
2. `mngr stop <host>` — container stopped but kept; build image retained. (A failed/crashed
   create reaches the same state without an explicit stop.)
3. Wait past `max_destroyed_host_persisted_seconds`.
4. `mngr destroy ...` (or any GC trigger) — its post-destroy GC sweeps the aged stopped host and
   calls `delete_host`, which raises the uncaught 409.

## Suggested fix

Remove the container in `delete_host` before untagging the build image — mirroring what
`destroy_host` already does. (`delete_host` not removing the container is also a latent container
leak for stopped/failed hosts.) Hardening `_remove_build_image` to pass `force=True`, and/or
catching `docker.errors.DockerException` in `_gc_single_host` so one host can't abort the whole
sweep, are reasonable defense-in-depth additions.

### Verification

With the container removed first, the build-image removal succeeds. I verified the
container-first fix below; the diff is provided as evidence of the root cause (not a proposed PR):

```diff
diff --git a/libs/mngr/imbue/mngr/providers/docker/instance.py b/libs/mngr/imbue/mngr/providers/docker/instance.py
--- a/libs/mngr/imbue/mngr/providers/docker/instance.py
+++ b/libs/mngr/imbue/mngr/providers/docker/instance.py
@@ -1493,6 +1493,17 @@ def delete_host(self, host: HostInterface) -> None:
             except (FileNotFoundError, OSError, MngrError) as e:
                 logger.trace("No host volume to clean up for {}: {}", host_id, e)
 
+        # Remove the container first so the build image is no longer referenced.
+        # delete_host runs for stopped/failed/crashed hosts whose container was
+        # never removed (destroy_host did not run), and a lingering container
+        # makes the untag below fail with a 409 conflict.
+        container = self._find_container_by_host_id(host_id)
+        if container is not None:
+            try:
+                container.remove(force=True)
+            except docker.errors.DockerException as e:
+                logger.warning("Error removing container: {}", e)
+
         # Defensive untag in case destroy_host did not run (idempotent).
         self._remove_build_image(host_id)
```

I also confirmed that `client.images.remove(tag, force=True)` succeeds while a container still
references the image: it removes the *tag* (stopping the image pileup that `_remove_build_image`
is meant to prevent) and keeps the underlying layers until the container is gone — so it does not
break the still-referenced container or any `docker commit` snapshot images.
