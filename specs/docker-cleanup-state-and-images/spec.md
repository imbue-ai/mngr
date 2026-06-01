# Docker cleanup: leaked build images + orphaned state containers

## Overview

- Two independent leaks in the Docker layer get fixed: per-host **build images** (`mngr`) and per-env **state containers** (`minds`).
- **Problem 1 (mngr docker provider):** `create_host` builds an image tagged `mngr-build-<host_id>`, but nothing ever removes that tag, so built images pile up in `docker images`.
- **Problem 2 (minds):** every minds env gets its own `MNGR_PREFIX`, so it gets its own Docker state container `<MNGR_PREFIX>docker-state-<user_id>` (with `restart_policy=unless-stopped`). When minds resets or destroys an env, that container is abandoned and runs forever.
- Fix scope is the lifecycle going forward only. We do **not** add a gc sweep for the images/containers that have already leaked (those get pruned manually).
- Two minds reset/destroy paths leak the state container today and both must clean it up: `minds env destroy` (`destroy_env`) and the activate-time generation-mismatch auto-wipe (`_check_generation_id_and_wipe_local_state_on_mismatch`).
- The minds-side teardown is deliberately **surgical**: it removes only the one exact container/volume named `<MNGR_PREFIX>docker-state-<user_id>` — never a broad prefix or label sweep — because over-matching would destroy unrelated state.

## Expected Behavior

### Problem 1 — build-image cleanup (mngr docker provider)

- After `mngr destroy <agent>` against a Docker host, the `mngr-build-<host_id>` tag is gone from `docker images`.
- After `delete_host` (gc of a destroyed host past its grace period), the tag is gone too (defensive second attempt; no-op if `destroy_host` already removed it).
- Hosts created from a pulled `--image` (no build step) have no `mngr-build-<host_id>` tag; the removal call is a harmless no-op for them.
- Snapshots keep working: untagging the build image does not break `mngr create --snapshot` or `start_host` from a snapshot (snapshot images are independent `docker commit` images and retain the underlying layers).
- Removal does not swallow errors: when the image is present, a genuine `docker rmi` failure propagates so it is visible (the image-absent case is a clean no-op, not a failure).

### Problem 2 — state-container cleanup (minds)

- `minds env destroy <name>`: after the env's mngr agents are destroyed, the env's exact state container and its backing named volume are removed before the env root is deleted.
- Activate-time auto-wipe (generation id changed): activating an env whose tier was destroyed + redeployed now (1) destroys that env's mngr agents, (2) removes that env's exact state container + volume, (3) wipes local `mngr/auth/logs`, (4) writes the new generation marker.
- The env's mngr agents are destroyed in a **single** `mngr destroy` call (host-mates and their containers/build-images all cleaned in one quick pass), not one call per agent.
- The Docker teardown targets only `<MNGR_PREFIX>docker-state-<user_id>`; `user_id` is read from the env's mngr profile (`<mngr_host_dir>/profiles/<profile>/user_id`) **before** any local rmtree removes that profile.
- Best-effort vs. raise: when Docker is absent / its daemon is unreachable (Modal- or imbue_cloud-only envs), the sweep is a silent no-op. When the container is already gone, that is success. When the container is present and `docker rm` fails, the sweep raises.
- When `user_id` cannot be resolved (e.g. an operator manually `rm -rf`'d the env root before `minds env destroy`), the state-container sweep is skipped with a warning — we cannot target the exact name, and we refuse to match anything broader.
- Production (`~/.minds/`) is unaffected: its destroy is hard-refused at the CLI, and only generation-tracking tiers (staging) hit the auto-wipe path.

## Implementation Plan

### mngr docker provider — `libs/mngr/imbue/mngr/providers/docker/instance.py`

- Add `_build_image_tag(host_id: HostId) -> str` returning `f"mngr-build-{host_id}"`. Replace the two inline `build_tag = f"mngr-build-{host_id}"` constructions in `create_host` with calls to it so the tag has a single source of truth.
- Add `_remove_build_image(host_id: HostId) -> None`:
  - Checks `self._docker_client.images.list(name=tag)`; if empty, it's a clean no-op (pulled-image host has no such tag, or a prior call already removed it) and returns.
  - Otherwise calls `self._docker_client.images.remove(tag)` and does NOT catch the result — a genuine removal failure propagates so the leak is visible rather than silently logged-and-ignored.
- `destroy_host`: after the container is removed, call `_remove_build_image(host_id)` (before/after `_mark_host_destroyed` — order is independent).
- `delete_host`: after the existing snapshot-image removal loop, call `_remove_build_image(host_id)` (defensive; idempotent with `destroy_host`).

### minds shared agent teardown — `apps/minds/imbue/minds/envs/mngr_agent_cleanup.py`

- Replace the per-agent `DestroyMngrAgentFn` / `real_destroy_mngr_agent` with a plural single-call form:
  - `DestroyMngrAgentsFn = Callable[[Sequence[str], Path, str, ConcurrencyGroup], None]`.
  - `real_destroy_mngr_agents(agent_ids, mngr_host_dir, mngr_prefix, cg)` runs one `mngr destroy -f <id1> <id2> ...` with `MNGR_HOST_DIR` / `MNGR_PREFIX` set (mngr's `destroy` takes `nargs=-1` agent ids). Keeps the existing "not found / does not exist" → success handling for the whole batch.
- `destroy_all_mngr_agents_in_env` enumerates ids as today, then invokes the injected plural callable **once** with the full id list (instead of looping). Returns the count. Preserves the "abort teardown on failure so cloud resources aren't stranded" contract.

### minds state-container cleanup — new `apps/minds/imbue/minds/envs/docker_cleanup.py`

- Stays subprocess-based and free of any `imbue.mngr` import (shells out to `docker`); only depends on `imbue.minds.bootstrap` helpers and stdlib + loguru + ConcurrencyGroup.
- Inlines the mngr conventions (intentionally not importing them, so we find out if they drift):
  - `_USER_ID_FILENAME: Final[str] = "user_id"`.
  - State container/volume name `= f"{mngr_prefix}docker-state-{user_id}"` (container and its named volume share this name).
- `read_profile_user_id(mngr_host_dir: Path) -> str | None`: read `<mngr_host_dir>/config.toml` → `profile` id → `<mngr_host_dir>/profiles/<profile>/user_id`; return `None` if config/profile/file missing (mirrors the resolution already done in `bootstrap._imbue_cloud_accounts_path`).
- `DockerCleanupError(MindError)` for real removal failures.
- `remove_state_container(*, container_name: str, parent_concurrency_group) -> None`:
  - Probe Docker availability (e.g. `docker container inspect <name>` / `docker ps -a`). `FileNotFoundError` (no `docker` CLI) or a daemon-unreachable error → log + return (skip).
  - Container absent → return (idempotent success).
  - `docker rm -f <name>` → non-zero (other than already-absent) raises `DockerCleanupError`.
  - `docker volume rm <name>` (after the container is removed, since Docker refuses to remove a mounted volume) → absent = success; present-but-fails = raise.
- `cleanup_env_state_container(name: DevEnvName, *, parent_concurrency_group) -> None`: resolve `root_name` via `root_name_for_env_name`, then `mngr_host_dir_for` + `mngr_prefix_for`; `user_id = read_profile_user_id(...)`; if `None` → warn + return; else build the exact name and call `remove_state_container`.

### minds `destroy_env` — `apps/minds/imbue/minds/envs/provisioning.py`

- Update the `Providers` bundle field `destroy_mngr_agent` → plural (`DestroyMngrAgentsFn`) to match the refactor.
- Add a new injected `Providers` field `cleanup_state_container: CleanupStateContainerFn = Callable[[DevEnvName, ConcurrencyGroup], None]`, wired to `docker_cleanup.cleanup_env_state_container` in `cli/env.py` (via a `_cleanup_state_container_for_provider` wrapper, like the other providers). This keeps `destroy_env` unit-testable with a fake and avoids real Docker in unit tests.
- Insert the state-container sweep immediately after Step 1 (mngr-agent teardown), **inside the same teardown branch**: call `providers.cleanup_state_container(name, parent_concurrency_group)`. It is skipped under `keep_agents=True` — kept agents still rely on the singleton state container. The "proceed on missing env root" behavior is honored via the `user_id`-unresolved → skip path.

### minds CLI wiring + auto-wipe — `apps/minds/imbue/minds/cli/env.py`

- Update the real-providers wiring: `destroy_mngr_agent=real_destroy_mngr_agent` → `real_destroy_mngr_agents` (plural).
- In `_check_generation_id_and_wipe_local_state_on_mismatch`, on a detected mismatch and *before* the `shutil.rmtree` of `env_root/{mngr,auth,logs}`:
  1. Resolve `root_name` → `mngr_host_dir` / `mngr_prefix`.
  2. Destroy the env's agents via one `real_destroy_mngr_agents(...)` call (enumerate ids with `list_agent_ids_in_env_root`); create a short-lived `ConcurrencyGroup` for this since the activate path has none today.
  3. `cleanup_env_state_container(env_name, parent_concurrency_group=...)` (reads `user_id` while the profile still exists).
  4. Then the existing rmtree + marker write.
- See Open Questions re: whether a sweep failure here should raise (tank activation) or be downgraded to a warning.

### Changelog

- Add per-PR changelog entries for both touched projects: `libs/mngr/changelog/mngr-cleanup-docker-containers.md` and `apps/minds/changelog/mngr-cleanup-docker-containers.md`.

## Implementation Phases

1. **mngr build-image untag.** Add `_build_image_tag` + `_remove_build_image`; wire into `destroy_host` and `delete_host`. Self-contained; fixes Problem 1 end to end.
2. **Single-call agent teardown.** Refactor `mngr_agent_cleanup` to the plural callable; update `provisioning.Providers` + `cli/env.py` wiring. System still works (destroy_env behaves as before, just one mngr call).
3. **minds state-container teardown module.** Add `docker_cleanup.py` (`read_profile_user_id`, `remove_state_container`, `cleanup_env_state_container`).
4. **Wire into `destroy_env`.** Insert the sweep after Step 1. Fixes the `minds env destroy` leak.
5. **Wire into the activate-time auto-wipe.** Add agent destroy + state-container sweep before the rmtree. Fixes the reset-on-generation-change leak.

## Testing Strategy

- **Build-image untag (mngr, `docker` marker, likely acceptance/release — needs a daemon):**
  - After `create_host` (default Dockerfile path) then `destroy_host`, assert `mngr-build-<host_id>` is absent from the daemon's image list.
  - After `delete_host`, assert absent and that a second removal is a no-op.
  - Pulled-`--image` host: `destroy_host` does not error and there was never an `mngr-build-*` tag.
  - Snapshot survives: create snapshot, `destroy_host` (untags build image), then `start_host`/`create --snapshot` still restores.
  - Reuse `make_docker_provider_with_cleanup`; generate unique names with `uuid4().hex`.
- **`read_profile_user_id` (minds unit):** tmp dir with `config.toml` + profile + `user_id` returns it; missing config / profile / file returns `None`.
- **State-container name construction (minds unit):** asserts exact `<prefix>docker-state-<user_id>` shape.
- **`remove_state_container` (minds, `docker` marker integration):** create a throwaway container+volume named per the template (unique `uuid4().hex` suffix), assert both removed; absent container = no-op; daemon-absent path (point at a bad `DOCKER_HOST`) = silent skip; rm-failure surfaces `DockerCleanupError`.
- **Single-call teardown (minds unit):** inject a fake `DestroyMngrAgentsFn` capturing args; assert it is called exactly once with the full id list.
- **`destroy_env` (minds unit, fake `Providers`):** assert the state-container cleanup hook is invoked after agent teardown and before `delete_env_root`; assert skip-with-warning when `user_id` unresolved.
- **Auto-wipe ordering (minds unit):** simulate a generation mismatch; assert order is destroy-agents → read `user_id` + remove container → rmtree → write marker, and that `user_id` is read before the rmtree.
- **Edge cases:** Modal-only env (no Docker) destroy/auto-wipe does not raise; production path never triggers a sweep.
- **Ratchets:** `docker_cleanup.py` uses `subprocess` to shell out to `docker`; if the broad-`subprocess` ratchet fires, follow the same documented-exclusion approach used by `mngr_agent_cleanup.py` / `desktop_client/destroying.py` rather than evading it.

## Open Questions (resolved during implementation)

- **Sweep failure during `activate`:** RESOLVED toward surfacing errors (per PR review). The activate-time wrapper `_destroy_agents_and_state_container_for_wipe` does NOT swallow teardown errors — a failure to destroy agents or remove the state container propagates so the operator sees it and can fix it, rather than silently leaking containers. (The sweep is still a clean no-op when there is simply no Docker daemon; only genuine removal failures raise.)
- **`keep_agents=True` in `destroy_env`:** RESOLVED to *skip* the state-container sweep under `keep_agents` — kept agents still rely on the singleton state container, so removing it would break them.
- **Batch "not found" handling for the single `mngr destroy` call:** RESOLVED — treat "not found" / "does not exist" anywhere in the combined output as success; any other non-zero exit raises `MngrAgentCleanupError`.
