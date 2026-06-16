# OVH VPS Provider Plugin (`mngr_ovh`)

## Overview

- New `mngr` provider plugin targeting OVH's **classic VPS product** (e.g. `vps-2025-model1` / "VPS-1" at ~$7.60/mo) — chosen over OVH Public Cloud because it's significantly cheaper for the long-running, pool-style workloads `mngr_imbue_cloud` will drive.
- Built on top of `mngr_vps_docker` and modeled closely on `mngr_vultr`. Each OVH VPS runs exactly one Docker container; agents live inside the container; the VPS itself stays running between agent sessions.
- Uses the official `python-ovh` SDK as the HTTP/auth layer (not raw `requests`), because OVH's signed-request auth is non-trivial and the SDK handles both OAuth2 and AK/AS/CK plus `~/.ovh.conf` discovery.
- Discovery uses OVH **IAM v2 universal tags** on the `vps` resource URN (`POST /v2/iam/resource/{urn}/tag`, `GET /v2/iam/resource?resourceType=vps`) — verified live to work for tagging, listing, and tag removal.
- OVH VPS has **no cloud-init / no `userData`**, so bootstrap differs from Vultr: order the VPS, then `POST /vps/{s}/rebuild` with `publicSshKey` + `doNotSendPassword=true` to pre-install our client key, then SSH in with key auth and TOFU-pin the host key. This is the only path; OVH's API exposes no host-key injection, no fingerprint endpoint, and no text-readable console.
- A small refactor lifts the parallel-SSH host-record discovery currently duplicated in `mngr_vultr` up into `VpsDockerProvider` (via a new `_list_provider_vps_hostnames()` seam method, concrete in the base with an empty default). `mngr_vultr` is migrated to the new base in the same PR. Cloud-init stays Vultr-specific; no two-implementation bootstrap interface yet — wait for a third provider to motivate that seam.

## Expected Behavior

### From the user's perspective
- `mngr create my-agent --provider ovh` provisions a fresh VPS-1, installs Docker (already in the `Debian 12 - Docker` image), runs the agent's container, and connects.
- `mngr list` shows OVH-backed hosts and their agents alongside hosts from any other provider, regardless of which machine the user is calling `mngr` from (discovery is server-side via IAM tags + SSH-reads of the state volume — no local-cache dependency).
- `mngr stop my-agent` stops the Docker container; the VPS keeps running. `mngr start my-agent` restarts the container.
- `mngr destroy my-agent` removes the container, deletes the state volume, terminates the VPS, and removes the IAM tags. README documents that destroying mid-month wastes the prorated remainder.
- Build args:
  - `--vps-plan=vps-2025-model1` (VPS-1 default), other valid OVH VPS plan codes
  - `--vps-datacenter=US-EAST-VA` (default for US accounts), `US-WEST-OR`
  - `--vps-os="Debian 12 - Docker"` (default, friendly name → resolved to image UUID at create time)
  - all other build args pass through to `docker build` on the VPS (same convention as Vultr)
- Start args pass through to `docker run` (same as Vultr).
- Snapshots: `mngr snapshot create <agent>` works but is capped to **one snapshot per VPS** by the OVH API; attempting a second snapshot returns a clear error.

### First-connect security model
- After `rebuild`, the very first SSH connection accepts the host key without prior verification (`StrictHostKeyChecking=accept-new` semantics), pins it, and enforces strict checking from then on.
- Because the rebuild already installed our SSH pubkey, key-auth is in force from connection #1 — a network MITM during the brief first-connect window can passively read the session but **cannot impersonate the VPS** (they don't have our private key).
- README explicitly documents this caveat and contrasts it with Vultr/Public-Cloud's cloud-init-injected host keys.

### Discovery semantics
- After `create_host` succeeds, the VPS carries IAM tags `mngr-provider=<provider_instance_name>` and `mngr-host-id=<host_id>`.
- `discover_hosts()` calls `GET /v2/iam/resource?resourceType=vps`, filters client-side for entries whose `tags["mngr-provider"]` matches this provider instance, then SSHes to each VPS in parallel (via the lifted base-class helper) to read host records and agent state from the state volume — same pattern as Vultr today.
- Tags die with the resource; no separate cleanup on `destroy_host` beyond destroying the VPS itself.

### Idle / cost behavior
- The existing `mngr_vps_docker` idle model is unchanged: idle agents → container stops (VPS stays up). No VPS-level cost saving from idle, but also no waste.
- Explicit `destroy` cleans up everything; the prorated month-remainder is forfeit. Acceptable because real production use will go through `mngr_imbue_cloud`'s VPS pool, which reinstalls the OS to recycle a VPS rather than destroying it.

## Implementation Plan

### New package: `libs/mngr_ovh/`

Mirrors the `libs/mngr_vultr/` layout exactly.

```
libs/mngr_ovh/
├── README.md                    # Usage, defaults, env vars, MITM-caveat note
├── conftest.py                  # `register_conftest_hooks(globals())`
├── pyproject.toml               # name=imbue-mngr-ovh; deps: imbue-mngr, imbue-mngr-vps-docker, ovh
└── imbue/mngr_ovh/
    ├── __init__.py              # `hookimpl = pluggy.HookimplMarker("mngr")`
    ├── backend.py               # OvhProvider, OvhProviderBackend, register_provider_backend
    ├── client.py                # OvhVpsClient (implements VpsClientInterface)
    ├── config.py                # OvhProviderConfig (extends VpsDockerProviderConfig)
    ├── bootstrap.py             # rebuild + TOFU-pin bootstrap flow
    ├── ordering.py              # /order/cart-based VPS purchase flow
    ├── catalog.py               # friendly-name → UUID resolution for images/datacenters/plans
    ├── iam_tags.py              # POST/GET/DELETE wrappers around /v2/iam/resource/{urn}/tag
    ├── backend_test.py
    ├── client_test.py
    ├── config_test.py
    ├── bootstrap_test.py
    ├── ordering_test.py
    ├── catalog_test.py
    ├── iam_tags_test.py
    ├── test_release_ovh.py      # @pytest.mark.release end-to-end tests
    └── test_ratchets.py
```

### New types

In `mngr_ovh/config.py`:
- `OvhProviderConfig(VpsDockerProviderConfig)` — Pydantic model with:
  - `backend: ProviderBackendName = "ovh"`
  - `endpoint: str = "ovh-us"` (python-ovh endpoint id; `OVH_ENDPOINT` env fallback)
  - `application_key: SecretStr | None`, `application_secret: SecretStr | None`, `consumer_key: SecretStr | None` (AK/AS/CK; `OVH_APPLICATION_KEY`/`OVH_APPLICATION_SECRET`/`OVH_CONSUMER_KEY` env fallbacks; **also** `OVH_APP_KEY`/`OVH_APP_SECRET` accepted as aliases for compatibility with how `infra@imbue.com` already names them)
  - `client_id: SecretStr | None`, `client_secret: SecretStr | None` (OAuth2; `OVH_CLIENT_ID`/`OVH_CLIENT_SECRET` env fallbacks)
  - `project_id: str | None` (not strictly required for classic VPS, but kept for future Public-Cloud compatibility; `OVH_PROJECT_ID` env fallback)
  - `default_region: str = "US-EAST-VA"` (override of base class default)
  - `default_plan: str = "vps-2025-model1"` (VPS-1)
  - `default_image_name: str = "Debian 12 - Docker"`
  - `pricing_mode: Literal["default", "upfront6", "upfront12"] = "default"`
  - `duration: str = "P1M"` (ISO-8601 — monthly billing only)
  - `vps_boot_timeout: float = 600.0` (override of base class 300s — orders are slow)
  - `get_credentials() -> dict[str, str]` returns the params python-ovh expects; precedence is explicit config > env vars > `~/.ovh.conf` > raises a clear `MngrError`.

In `mngr_ovh/client.py`:
- `OvhVpsClient(VpsClientInterface)` — wraps `ovh.Client`. Holds the `endpoint` + resolved credentials. Methods:
  - `create_instance(label, region, plan, os_id, user_data, ssh_key_ids, tags)` — runs the order-and-rebuild flow; `user_data` and `ssh_key_ids` are accepted (for interface compat) but `user_data` is ignored (OVH has no userData field) and `ssh_key_ids` is treated as a list of public keys to install via `publicSshKey` during rebuild. Returns the `VpsInstanceId` (= OVH `serviceName` like `vps-eec8860b.vps.ovh.us`).
  - `destroy_instance(instance_id)` — calls `POST /vps/{s}/terminate` then `POST /vps/{s}/confirmTermination`.
  - `get_instance_status / get_instance_ip / wait_for_instance_active / list_instances` — wrappers around `GET /vps/{s}` and `GET /vps`.
  - `upload_ssh_key / delete_ssh_key` — VPS rebuild takes inline pubkeys, so these become no-ops that return synthetic IDs (or raise `NotImplementedError` with a clear message that OVH VPS doesn't use a separate key store). To preserve the `VpsClientInterface` contract without weird semantics, we instead have `upload_ssh_key(name, public_key)` cache `public_key` in memory keyed by `name` and return `name` as the ID; `create_instance` then resolves the ID back to the key for `publicSshKey`. No round-trip to OVH for keys.
  - `wait_for_task(task_id, timeout_seconds)` — polls `GET /vps/{s}/tasks/{taskId}` until `state in {"done", "error", "cancelled", "blocked"}`; raises on terminal-error states.

In `mngr_ovh/bootstrap.py`:
- `bootstrap_vps_via_rebuild(vps_client, service_name, image_id, our_public_key) -> str` — runs `POST /vps/{s}/rebuild` (`publicSshKey=our_public_key`, `doNotSendPassword=true`, `imageId=image_id`), polls the returned task to completion, returns the rebuild-completion timestamp for logging.
- `pin_host_key_on_first_connect(vps_ip, known_hosts_path, ssh_user, ssh_key_path, expected_attempts=10, backoff=2.0) -> str` — opens an SSH connection with `StrictHostKeyChecking=accept-new`, retries on connection refused (rebuild leaves sshd unavailable for ~30 s), writes the discovered host key into `known_hosts_path`, returns the public host key string for storage in the VPS host record.

In `mngr_ovh/ordering.py`:
- `order_vps(ovh_client, plan_code, datacenter, image_name, pricing_mode, duration, subsidiary="US") -> str` — runs the full cart flow (`POST /order/cart` → `POST /cart/{id}/vps` → `POST /cart/{id}/item/{itemId}/configuration` for `vps_datacenter` + `vps_os` → `POST /cart/{id}/assign` → `POST /cart/{id}/checkout`). Returns the `serviceName` once the resulting VPS appears in `GET /vps`. Polls `GET /vps` with backoff up to `vps_boot_timeout`.
- Internal helper `_set_required_configurations(ovh_client, cart_id, item_id, fields: Mapping[str, str])` does the `requiredConfiguration` walk.

In `mngr_ovh/catalog.py`:
- `resolve_image_id(ovh_client, service_name, image_name) -> str` — `GET /vps/{s}/images/available` + name match.
- `resolve_plan_code(ovh_client, plan_alias) -> str` — pass-through today; accepts `vps-2025-model1` or friendlier `VPS-1` and maps to plan codes.
- `resolve_datacenter(ovh_client, plan_code, subsidiary) -> str` — validates against the `requiredConfiguration` allowed values; returns the canonical datacenter code.

In `mngr_ovh/iam_tags.py`:
- `attach_tag(ovh_client, urn, key, value) -> None`
- `delete_tag(ovh_client, urn, key) -> None`
- `list_vps_resources(ovh_client) -> list[IamResource]` — `GET /v2/iam/resource?resourceType=vps`; client-side filtering (since `?tags[k][value]=v` returns 400).
- `IamResource` is a small `FrozenModel` with `urn`, `name`, `display_name`, `tags: Mapping[str, str]`.

In `mngr_ovh/backend.py`:
- `OvhProvider(VpsDockerProvider)` — implements the new `_list_provider_vps_hostnames()` seam by querying `iam_tags.list_vps_resources` filtered by `tags.get("mngr-provider") == self.name`, then returning each matching ``serviceName`` (which doubles as the VPS's DNS hostname). Caches the IAM list per-command (same pattern as `VultrProvider._list_instances_cached`).
- `OvhProviderBackend(ProviderBackendInterface)` — name `"ovh"`, build-args help, config class. `build_provider_instance` constructs `OvhVpsClient` from `OvhProviderConfig.get_credentials()`.
- `@hookimpl register_provider_backend()` returns the tuple.

### Refactor of `mngr_vps_docker`

In `libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py`:
- Add a new provider-specific seam method on `VpsDockerProvider`:
  ```
  def _list_provider_vps_hostnames(self) -> list[str]:
      return []
  ```
  Concrete in the base with an empty default so tests and providers without a listing API can opt out; concrete providers override.
- Move the following from `VultrProvider` (in `libs/mngr_vultr/.../backend.py`) into `VpsDockerProvider` as concrete (non-abstract) methods, using `_list_provider_vps_hostnames()` as the only provider-specific seam:
  - `_read_records_from_vps(vps_ip)` (unchanged body)
  - `_discover_host_records_with_agents()` (uses `_list_provider_vps_hostnames()` instead of the Vultr-specific `_get_tagged_vps_ips`)
  - `_discover_host_records()` (becomes a one-liner over the above)
  - `_find_host_record(host)` (cache-first then falls through to `_discover_host_records`)
- Concurrency-group name in the lifted method is parameterized: `f"{type(self).__name__}-discover"`.

In `libs/mngr_vultr/imbue/mngr_vultr/backend.py`:
- `VultrProvider` becomes much smaller — only implements `_list_provider_vps_hostnames()` and `reset_caches()` (to wipe the new `_instances_cache` field plus call `super().reset_caches()`).
- Delete `_get_tagged_vps_ips`, `_read_records_from_vps`, `_discover_host_records_with_agents`, `_discover_host_records`, `_find_host_record`.
- Implementation of `_list_provider_vps_hostnames()` is the body of the current `_get_tagged_vps_ips()`.

### Plugin registration

In `libs/mngr_ovh/pyproject.toml`:
```toml
[project.entry-points.mngr]
ovh = "imbue.mngr_ovh.backend"
```

Workspace registration: add `libs/mngr_ovh` to the root `pyproject.toml`'s workspace members (alongside `libs/mngr_vultr`).

### Dependencies
- `ovh>=1.0` (python-ovh SDK) — adds OAuth2/AK-AS-CK signing for free; supports `~/.ovh.conf`; allows arbitrary `/v2/...` paths.
- No other new third-party deps.

## Implementation Phases

Each phase ends with a working (if incomplete) system that can be merged independently if needed.

### Phase 1 — Refactor `mngr_vps_docker` discovery, keep Vultr green
- Add `_list_provider_vps_hostnames()` seam method (concrete in the base, `return []` default) to `VpsDockerProvider`.
- Lift `_read_records_from_vps`, `_discover_host_records_with_agents`, `_discover_host_records`, `_find_host_record` from `VultrProvider` to `VpsDockerProvider`.
- Slim down `VultrProvider` to implement only `_list_provider_vps_hostnames()` + the cache helpers.
- All existing `mngr_vultr` unit/integration/release tests continue to pass unchanged.
- No new functionality.

### Phase 2 — Skeleton `mngr_ovh` package
- Create `libs/mngr_ovh/` directory tree with empty stubs.
- `pyproject.toml`, `conftest.py`, `__init__.py`, `README.md`.
- Register entry-point.
- `OvhProviderConfig` with credential resolution; `OvhProviderBackend` stub that raises `NotImplementedError` on `build_provider_instance`.
- Unit tests for config credential resolution (env / explicit / `~/.ovh.conf` precedence; missing-credential error).
- `mngr plugin list` shows `ovh` as a registered provider.

### Phase 3 — `OvhVpsClient` API surface against the live API
- Implement `client.py`: thin wrappers around python-ovh for `GET /vps`, `GET /vps/{s}`, `POST /vps/{s}/start/stop/reboot`, `POST /vps/{s}/createSnapshot`, `DELETE /vps/{s}/snapshot`, etc.
- Implement `wait_for_task` task-polling helper.
- Unit tests with mocked `ovh.Client.{get,post,delete}`.
- Release-test stub (`@pytest.mark.release`) that exercises read-only endpoints (`list_instances`).

### Phase 4 — Ordering and bootstrap
- Implement `ordering.py`: cart flow with full datacenter+OS configuration walk.
- Implement `catalog.py`: image-name resolution.
- Implement `bootstrap.py`: rebuild + TOFU-pin SSH host-key acquisition. Uses paramiko's `AutoAddPolicy` semantics scoped to a per-host `known_hosts` file (not the user's global `~/.ssh/known_hosts`).
- Unit tests for the cart flow (mock python-ovh) and the bootstrap helpers.

### Phase 5 — IAM tag wiring + provider integration
- Implement `iam_tags.py`.
- Wire it into `OvhProvider`: tag attach in `create_host` after VPS is ready; tag-based discovery in `_list_provider_vps_hostnames`.
- Unit tests for IAM tag wrappers + discovery filtering.

### Phase 6 — End-to-end and release tests
- `test_release_ovh.py` exercises `mngr create/exec/list/stop/start/destroy --provider ovh` against the real OVH API (mirrors `test_release_vultr.py`).
- Gate on `OVH_APPLICATION_KEY` / `OVH_APP_KEY` env var being set.
- Add release-test variants for snapshot create/delete.
- Manually verify the full flow once, then declare done.

## Testing Strategy

### Unit tests (`*_test.py`)
- **`config_test.py`** — credential precedence (explicit > env > `~/.ovh.conf` > error); env-var aliasing (`OVH_APP_KEY` vs `OVH_APPLICATION_KEY`); default values; backend name.
- **`client_test.py`** — every `OvhVpsClient` method with a mocked `ovh.Client`: success cases, OVH API error mapping to `VpsApiError`, snapshot single-slot enforcement, task polling state transitions.
- **`ordering_test.py`** — full cart-flow happy path; missing-required-configuration error; `requiredConfiguration` parser correctness; subsidiary-mismatch handling.
- **`bootstrap_test.py`** — `bootstrap_vps_via_rebuild` issues the right `POST /vps/{s}/rebuild` body; polls until done; surfaces task errors; `pin_host_key_on_first_connect` retries on `ECONNREFUSED`, writes one entry to the per-host `known_hosts` file, returns the public host key.
- **`catalog_test.py`** — image-name resolution; unknown name raises a clear error.
- **`iam_tags_test.py`** — attach/delete/list wrappers issue the right HTTP calls; the client-side tag-filter helper isolates per-provider VPSes correctly.
- **`backend_test.py`** — same shape as `vultr/backend_test.py`: registration tuple, name, description, config class, build-args help.

### Integration tests (`test_*.py`, no marker)
- `test_provider_smoke.py` — instantiate `OvhProvider` against a mock OVH server (a tiny in-process HTTP server) and run `discover_hosts` with no VPSes; assert empty result and no errors.

### Release tests (`@pytest.mark.release`)
- `test_release_ovh.py::TestOvhVpsClient` — read-only methods against the live API (list instances, list snapshots).
- `test_release_ovh.py::TestOvhProviderLifecycle::test_create_exec_and_destroy` — full create-exec-destroy loop, mirrors Vultr.
- `test_release_ovh.py::TestOvhProviderLifecycle::test_create_stop_start_destroy` — container stop/start loop.
- `test_release_ovh.py::TestOvhProviderLifecycle::test_ssh_connectivity` — verify post-bootstrap SSH host-key pinning and OS detection inside container.
- All release tests skipped unless `OVH_APPLICATION_KEY` (or `OVH_APP_KEY`) is set.

### Edge cases worth explicit tests
- Rebuild task transitions to `error` → `OvhVpsClient.wait_for_task` raises `VpsProvisioningError` with the task type and ID in the message.
- IAM tag attach returns 404 for a not-yet-visible VPS (race after provisioning) → retry with backoff up to 5 attempts before giving up.
- Two VPSes in the same project from two different `mngr` provider instances (`name=alice-ovh`, `name=bob-ovh`) → each instance's discovery only returns its own VPSes.
- `~/.ovh.conf` exists but has a syntax error → falls back to env / explicit config rather than crashing on import.
- Manual verification: stand up one VPS, run an agent, check `mngr list` from a second machine that has the same OVH credentials but a fresh local profile — confirm the agent appears (proves IAM-tag-based discovery is truly server-side).

### Ratchet tests
- `test_ratchets.py` — copy `mngr_vultr/.../test_ratchets.py` verbatim and adjust the snapshot counts as the new code lands. Same set of ratchet checks.

## Open Questions

1. **Should the `VpsClientInterface.upload_ssh_key` / `delete_ssh_key` methods become optional (e.g. raise `NotImplementedError` and have the base class gate behavior accordingly), or do we stick with the "treat as in-memory cache, return a synthetic ID" hack proposed in the plan?** The hack keeps the interface unchanged at the cost of slight semantic weirdness. Cleanest answer is probably to refactor the interface to take an inline `public_keys: Sequence[str]` in `create_instance` and drop the upload/delete methods entirely — but that's a larger ripple than this PR wants.
2. **Where does the per-VPS `known_hosts` file live, and is it cleaned up on `destroy`?** Plan assumes `{profile_dir}/providers/ovh/{name}/known_hosts/{service_name}` and that `destroy_host` removes that file. Worth confirming the directory structure matches what `mngr_vps_docker`'s SSH-utils code expects.
3. **Should we surface a `--vps-pricing-mode=upfront12` build arg so users can opt into the 16% discount on long-running pools, or is that purely a config-file knob?** Build-arg gives per-host control; config-file is simpler. Default to config-file, follow up if real pool usage demands per-host overrides.
4. **`OvhProviderConfig.project_id` is currently optional (only needed for Public Cloud, which we're not implementing yet). Drop it from the config entirely, or keep the field as a no-op so a future `mngr_ovh_cloud` plugin could read the same config?** Recommend keep, document as "reserved for future Public Cloud support."
5. **Local Zone variants (`vps-2025-model1.LZ`) vs base variant** — the user's test VPS was on the LZ variant (Palo Alto). Should the README call out the LZ vs base distinction prominently, or just point at OVH's plan-code list?
6. **First-connect TOFU acceptability for `mngr_imbue_cloud` workloads** — for the eventual pool use case, an attacker positioned in the network during the rebuild→first-SSH window could passively read the SSH session. Is that an acceptable threat model for production pool agents, or do we need to revisit (e.g., implement VNC-OCR host-key extraction) before pool rollout?
7. **`upload_ssh_key` interface mismatch with the lifted base-class code** — once Phase 1 lands and `mngr_vps_docker` calls `upload_ssh_key` generically, does our in-memory-only OVH impl cause any subtle bug (e.g., re-instantiating the provider between calls would lose the cache)? Mitigation: have `OvhVpsClient` persist the keyed map to a small file under the provider profile dir, or short-circuit by detecting that `ssh_key_ids[0]` already looks like a literal pubkey and using it directly.
