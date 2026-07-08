# Plan: Fix leaked Docker state containers from local test runs

## Overview

- Root cause is two independent bugs that compound:
  - The Docker provider's singleton **state container** is created lazily by *any* operation that touches `_state_volume` — including read-only `discover_hosts` (via `_host_store.list_all_host_records()`). So `mngr list`/`gc`/`cleanup`/discovery spin up a persistent state container even with zero docker hosts, and tests running `mngr` subprocesses under the bare autouse env leak one each (confirmed: four `mngr_<uuid>-docker-state-<user_id>` containers, `provider=docker`, up ~41h).
  - The session-end safety net's `_looks_like_test_prefix` has an off-by-one (`container_name[4:]` strips only `mngr`, not `mngr_`), so it never matches `mngr_<hex32>-` test containers and never sweeps them.
- Primary fix mirrors the existing Modal pattern: thread `is_for_host_creation` into the Docker backend so the state container is the Docker analog of the Modal app/environment — created only when actually creating a host, never for read-only commands.
- Secondary fix: correct the cleanup-detection off-by-one and make the safety net able to attribute leaks to the current worker (via a registry the docker fixtures populate), failing only on our own leaks while warn-cleaning unrelated/old orphans.
- Behavior change is intentional and desirable in production too: read-only commands no longer materialize a state container on a machine with no docker hosts.

## Expected behavior

- `mngr list`, `mngr gc`, `mngr cleanup`, and any cross-provider discovery no longer create a Docker state container when one does not already exist; the docker provider is simply skipped (treated as empty), exactly like Modal skips a provider whose environment doesn't exist.
- The docker provider is considered non-empty only when its state container already exists; in that case read-only commands use it normally (existing hosts are listed/operated on as before).
- `mngr create --provider docker` (the only path that passes `is_for_host_creation=True`) creates the state container as needed and host creation works exactly as today.
- When the Docker daemon is unreachable during the emptiness check, the provider is skipped via `ProviderUnavailableError` (unchanged user-visible behavior for an offline daemon).
- Existing-host operations (`destroy`, `stop`, `start`, `snapshot`, gc `delete_host`) continue to work, since a host that exists implies its state container exists.
- Test runs no longer leak state containers: tests that create docker hosts do so only through the dedicated cleanup fixtures, which remove their own containers/volumes; the bare autouse subprocess path no longer creates any.
- The session-end safety net now recognizes leaked test state containers: it fails the suite (after cleaning) when a container created under one of this worker's registered prefixes survives, and only warn-and-cleans containers it cannot attribute to this worker (parallel/xdist-safe).
- The four currently-leaked containers are left in place; the corrected safety net will sweep them on a subsequent run once they fall outside any registered prefix (age-based warn-and-clean).

## Changes

- **Docker backend (`providers/docker/backend.py`)**: stop discarding `is_for_host_creation`; when it is `False`, check whether the state container already exists and raise `ProviderEmptyError` if not (and `ProviderUnavailableError` if the daemon is unreachable), so the provider loader skips it — mirroring the Modal backend. When `True`, build the instance normally.
- **Docker provider/state-container layer (`providers/docker/instance.py`, `providers/docker/volume.py`)**: support a "non-empty means the state container already exists" check without creating it, and ensure the create-allowed path is used only for host creation. Read-only discovery must not be the thing that creates the container.
- **Cleanup-detection helpers (`conftest.py` → `utils/testing.py`)**: move `_looks_like_test_prefix` / stale-detection helpers into `utils/testing.py` (so they can be unit-tested), and fix the off-by-one so `mngr_<hex32>-` names are recognized.
- **Worker-level registry (`conftest.py` + `utils/testing.py`)**: add a per-worker list (alongside `worker_test_ids`) of docker-state container prefixes; the docker fixtures (`docker_provider`, `docker_subprocess_env`) append their prefix on setup. The docker fixtures remain the only sanctioned way to create docker state.
- **Session cleanup (`conftest.py` `session_cleanup`)**: after tests, fail the suite (after cleaning) on any surviving state container whose name matches a registered prefix; warn-and-clean unrecognized/old orphans without failing.
- **Fixtures (`providers/docker/conftest.py`, `providers/docker/testing.py`)**: left functionally unchanged except for registering their prefix into the new registry.
- **Tests**: add a `docker_sdk` regression test asserting that (1) constructing the provider with `is_for_host_creation=False` raises `ProviderEmptyError` when no state container exists, and (2) a read-only operation leaves zero state containers. Audit (one-time, manual) that every docker test uses a cleanup fixture.
- **Changelog**: add a single `libs/mngr` changelog entry noting that read-only commands no longer create a docker state container (no user-facing docs rewrite).
- **Out of scope**: manually removing the four already-leaked containers; automated enforcement of fixture usage; user-facing docs rewrite.
