# Spec uncertainties

Conflicts or potentially-outdated information found while writing specs, to be
resolved later. Each entry: the conflict, where it is, and the assumption made.

## VPS Docker "single mode of operation" vs the bare/docker realizer axis

- **Where:** `specs/vps-docker-provider/spec.md` ("Single mode of operation"
  section) asserts the VPS providers have one mode: the VPS always runs and the
  Docker container is the host, with `docker stop`/`docker commit` as the
  stop/snapshot primitives.
- **Conflict:** `specs/bare-providers/spec.md` introduces a *bare* realization
  (agent directly on the VM, no container) selected by `config.mode`, and the
  instance-stop lifecycle (`specs/aws-ec2-stop-start-lifecycle/spec.md`) already
  added a machine-stop path that the "single mode" framing predates.
- **Assumption made:** the "single mode" statement describes the original Docker
  shape, not an invariant. The bare spec treats realization as an explicit axis and
  the Docker shape as one (default) point on it. When the bare work lands, update
  `specs/vps-docker-provider/spec.md` to reference the realizer axis.

## `mngr start` comment claims start_host routing that the code does not do

- **Where:** `libs/mngr/imbue/mngr/cli/start.py:235-237` comment: "Ensure host is
  started (always start since this is the start command). start_host is idempotent
  (returns early if the host is already running), so concurrent starts do not need
  to coordinate around this step."
- **Conflict:** the comment describes routing through `start_host` unconditionally,
  but the following line calls `ensure_host_started(host, is_start_desired=True, ...)`
  (`libs/mngr/imbue/mngr/api/find.py:326`), which returns an already-online `Host`
  untouched and only calls `provider.start_host` on the offline branch. So
  `start_host` never runs for a host the provider classified online, and its two
  premises are also false in general: lima's `start_host` is not idempotent
  (`libs/mngr_lima/imbue/mngr_lima/instance.py:741` re-runs record rewrites and
  restarts the in-VM activity watcher), and the SSH provider's `start_host` raises
  `NotImplementedError` (`libs/mngr/imbue/mngr/providers/ssh/instance.py:197`).
- **Assumption made:** the comment is aspirational / stale and the code is
  authoritative. `specs/host-start-readiness-race.md` analyzes this seam and
  recommends a fix; that spec is the resolution of this entry.
