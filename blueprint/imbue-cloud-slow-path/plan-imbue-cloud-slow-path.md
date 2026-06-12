# imbue_cloud robust slow-path host leasing

## Overview

* Today imbue_cloud leasing is **fast-path only**: `mngr create ...@host.imbue_cloud_<slug> --new-host` calls `client.lease_host(attributes)`, which the connector matches with `attributes @> %s::jsonb`. If no pool host matches the requested `repo_branch_or_tag`, the connector returns 503 and the create fails outright — there is no recovery.
* Add a **slow path**: when the fast path is not used (or no exact match exists), lease *any* available pool host, destroy its pre-baked Docker container, and have the client rebuild the host from the FCT Dockerfile — i.e. treat the leased VPS exactly like an OVH host. The client owns the machine the moment the connector marks it `leased`.
* Both paths run inside `ImbueCloudProvider.create_host`, gated by a new `fast_mode` knob with two values: `require` (fast/adopt only) and `prevent` (slow/full-rebuild only). The provider default is `prevent`.
* The slow path delegates to a **single canonical "build + set up host on an already-created VPS"** method in `mngr_vps_docker`. `VpsDockerProvider.create_host` is refactored to call that same method after it orders a VPS, so there is exactly one host-setup code path and no duplication / skew between a freshly-baked pool host and a slow-path rebuild.
* Reliability invariant: once a lease is obtained, **any** subsequent failure releases the host back to imbue_cloud (plain `release_host`, no wipe) before re-raising. Failed builds never leak a paid lease.
* The path actually taken is reported clearly in provider-side logs (no minds UI change).

## Expected behavior

* **`fast_mode=require`** (minds' first call): leases with the **exact** requested attributes (incl. `repo_branch_or_tag`). On a match, adopts the pre-baked `system-services` agent as today (no transfer, minimal provision). On no match, raises a new, distinct `FastPathUnavailableError` so the caller can react.
* **`fast_mode=prevent`** (provider default; minds' second call): leases with **relaxed** attributes — drops `repo_branch_or_tag`/`repo_url`, keeps `cpus`/`memory_gb`/`gpu_count` — so it grabs any adequately-sized available host. It then destroys the baked container on the leased VPS and rebuilds it from the FCT Dockerfile via the shared vps_docker setup method, after which mngr's standard create pipeline does the full client-side agent setup (transfer code to `/mngr/code`, fresh agent state, full provision, start).
* **Pool genuinely empty**: even the relaxed `prevent` lease can 503; that surfaces as the existing `ImbueCloudLeaseUnavailableError` ("pool exhausted"), distinct from `FastPathUnavailableError`. No lease was obtained, so nothing is released.
* **Failure after a lease (either path)**: the provider calls `release_host(host_db_id)` best-effort (no privacy wipe — nothing sensitive exists yet and the VPS may be unreachable), logs it, then re-raises the original error.
* **Identity stays aligned with the lease**: the rebuilt container reuses the pool row's pre-baked `host_id`/`agent_id`, so `mngr list`/discovery and the connector's lease row stay consistent. Because the rebuilt container has no on-disk agent `data.json` yet, the existing `ImbueCloudHost` fallback paths (`create_agent_state`/`create_agent_work_dir`/`provision_agent`) naturally run the full `super()` setup while pinning the agent to the pre-baked id.
* **Image/version semantics**: the slow path produces the correct version because it actually rebuilds the container image from the supplied FCT Dockerfile/build-context — not the pre-baked image that happened to be available.
* **minds**: launching an imbue_cloud workspace first runs `mngr create` with `fast_mode=require` and no `--project`. If that raises `FastPathUnavailableError`, minds clones the FCT repo at the resolved branch/tag and runs a second `mngr create` with `fast_mode=prevent` and `--project <clone>`. Other launch modes (DOCKER/LIMA/CLOUD) are unchanged.
* **`mngr` CLI users**: a bare `mngr create ...@host.imbue_cloud_<slug> --new-host` now defaults to `prevent` (robust full rebuild) using the source resolved by `mngr create`; if that source can't serve as an FCT build context the build fails and the lease is released, with a clear error. Adopting the baked host is opt-in via `-b fast_mode=require`.
* **Logs**: provider emits an unmistakable marker per path, e.g. `imbue_cloud FAST PATH: adopted pre-baked agent on leased host <id>` vs `imbue_cloud SLOW PATH: no fast match; re-leased <id> with relaxed attributes and rebuilding container`.

## Changes

### `mngr_vps_docker` (single canonical host-setup path)

* Refactor `VpsDockerProvider.create_host` so the post-ordering work (btrfs prep, bind volume, image build, container run, container SSH, finalize/certified-data) is extracted into one **public, credentials-free** method, e.g. `create_host_on_existing_vps(...)`, that operates purely over root SSH to an already-reachable VPS and makes **no** VPS-client (ordering) calls.
* `create_host` becomes: order VPS (existing logic) → call `create_host_on_existing_vps(...)`. This guarantees the slow-path rebuild and a normal vps_docker/ovh bake share identical setup code.
* The method must accept an explicit `host_id` (and the inputs needed to label the container, build from a Dockerfile build context, map `container_ssh_port`, create the per-host btrfs subvolume + `mngr-host-vol-<hex>` named volume, and write certified host data), so callers can pin identity.
* Add a changelog entry under `libs/mngr_vps_docker/changelog/`.

### `mngr_imbue_cloud` (the fast/slow orchestration)

* **Depend on `mngr_vps_docker`** (workspace dep), mirroring how `mngr_ovh` does.
* **`fast_mode` knob**: add a two-value enum (`require`, `prevent`) as an `UpperCaseStrEnum`-style type in `primitives.py`; default `prevent`.
* **`-b` parsing** (`LeaseAttributes.from_build_args`): split recognized control/lease keys (`account`, `fast_mode`, `repo_url`, `repo_branch_or_tag`, `cpus`, `memory_gb`, `gpu_count`) from everything else; return the leftover args as **pass-through build args** for the delegated vps_docker build instead of rejecting unknown keys. (Update its return tuple/shape and all callers/tests accordingly.)
* **`errors.py`**: add `FastPathUnavailableError(ImbueCloudError)` for the `require`-miss case (kept distinct from `ImbueCloudLeaseUnavailableError`, which remains the genuine "pool exhausted" signal).
* **`ImbueCloudProvider.create_host`** becomes the path selector:
  * Parse `fast_mode` + relaxed/exact attributes + pass-through build args.
  * `require`: lease with exact attributes. On 503 → `FastPathUnavailableError`. Otherwise wait for sshd, host-key scan, rewrite `data.json` host name, and return the adopting `ImbueCloudHost` as today.
  * `prevent`: lease with relaxed attributes (drop `repo_branch_or_tag`/`repo_url`). Then, over the lease's root SSH (`outer_host_for`), **destroy the pre-baked container** for the lease's `host_id` (reuse the existing container-removal helper / `mngr_vps_docker` teardown). Synthesize a `VpsDockerProviderConfig` from imbue_cloud config fields (`container_ssh_port=2222`, `host_dir=/mngr`) + vps_docker defaults, construct the delegated provider, and call `create_host_on_existing_vps(host_id=lease.host_id, name=name, build_args=<pass-through>, ...)` to rebuild. Return an `ImbueCloudHost` carrying the pre-baked `host_id`/`agent_id` and `lease_db_id` so the standard create pipeline runs full setup against the rebuilt container.
  * Wrap everything after the lease in a guard that, on any exception, calls `client.release_host(token, host_db_id)` (best-effort, logged, no wipe) and re-raises.
  * Emit the FAST/SLOW path log markers.
* **Logging**: clear `logger.info` markers distinguishing the two paths (and the relaxed re-lease).
* Add a changelog entry under `libs/mngr_imbue_cloud/changelog/`.
* Update `libs/mngr_imbue_cloud/README.md` to document fast vs slow paths and `fast_mode`.

### `minds` (two-call driver)

* In `agent_creator.py`, for `LaunchMode.IMBUE_CLOUD`: issue the first `mngr create` with `-b fast_mode=require` and **no** `--project`.
* Catch `FastPathUnavailableError` (surfaced via the mngr CLI failure path; match on the distinct error/marker) and, on that signal only, clone the FCT repo at the resolved branch/tag, then issue a second `mngr create` with `-b fast_mode=prevent`, `--project <clone>`, and the pass-through build args needed for the rebuild. Other lease failures (e.g. `ImbueCloudLeaseUnavailableError`) still fail the creation.
* Add a changelog entry under `apps/minds/changelog/` and update `apps/minds/docs/host-pool-setup.md` to describe the two-call behavior.

### forever-claude-template (`~/project/forever-claude-template`)

* Extend `[create_templates.imbue_cloud]` in `.mngr/settings.toml` to carry the ovh-style build inputs that the slow path passes through to the vps_docker build — `target_path = "/mngr/code/"`, `build_arg__extend = ["--file=Dockerfile", "."]`, and `post_host_create_command__extend = ["/usr/local/bin/fct-seed"]` — while keeping `idle_mode = "disabled"` and the existing `pass_host_env__extend`. The fast/adopt path ignores these; the slow path forwards them.
* Update the template comment to explain fast vs slow usage. (FCT is a separate repo — work via a worktree per CLAUDE.md; changelog handled there / under `dev` as applicable.)

### Testing (high level; details out of scope for this concise plan)

* Unit-test `LeaseAttributes.from_build_args` split (control keys vs pass-through; `fast_mode` parsing + default `prevent`).
* Unit-test the relaxed-attribute derivation (drops `repo_branch_or_tag`/`repo_url`, keeps resources).
* Unit/acceptance-test the release-on-failure guard (a forced failure after lease triggers exactly one `release_host`).
* Acceptance-test both paths end-to-end against a connector/pool fixture: `require` adopts; `require` miss → `FastPathUnavailableError`; `prevent` rebuilds via the shared vps_docker method and yields a fully set-up agent.
