# Workspace resource resizing (CPU + memory) for local minds

## Overview

* Local minds are stuck with their creation-time resources (lima: 4 CPU / 4 GiB lima defaults; docker: unlimited). There is no way to change them without recreating the workspace. This adds resize support for CPU and memory, surfaced in the minds per-workspace settings page. Disk resizing is out of scope.

* Resizing becomes a generic provider capability on `ProviderInstanceInterface` (default: unsupported), described by a rich descriptor: which dimensions are resizable, whether changes apply live or on next restart, minimums, provider defaults, and per-provider physical ceilings. Lima and docker implement it now; the shape leaves room for remote providers (e.g. imbue_cloud) later. UI and CLI drive entirely off the descriptor.

* Responsibilities split cleanly: `mngr limit` is a pure setter (persists desired values, applies live where possible, never restarts); the minds app decides when to restart, reusing its existing host-scope restart operation machinery.

* Staleness is detected by probing, not cached flags: desired values live in mngr's durable host record; actuals are read exactly from `limactl list --json` (lima) and `docker inspect` (docker). Any discrepancy means "pending restart." Guest-side probing was ruled out empirically: a 4 GiB lima VM reports MemTotal of ~3.81 GiB (kernel reserves ~110 MiB + ~1.4% of size), so exact comparison from inside the guest is unreliable at large sizes.

* Key lima mechanics (verified on real VMs): `limactl edit --cpus N --memory N` cleanly rewrites an instance's config but refuses while the VM runs; CPU/memory only take effect at boot. So lima applies the desired values via `limactl edit` inside `start_host` (the VM is stopped at exactly that point), guaranteeing a running VM's `limactl list` output always reflects what it actually booted with. Docker applies live via `docker update`.

## Expected behavior

### mngr CLI (`mngr limit`)

* `mngr limit --host X --cpus N` / `--memory N` (integer GiB) sets desired resources; first-class, documented flags. Output reports the configured values, the actual (probed) values, and whether a restart is needed to apply.

* Targeting an agent (`mngr limit my-agent --memory 8`) applies to the agent's underlying host, consistent with existing host-level settings like `--idle-timeout`.

* `mngr limit --host X` with no setting flags becomes a read mode: reports the resize capability descriptor, configured values, and actual values (JSON output for minds to consume).

* Requesting values above the provider's ceiling (lima: machine physical cores/RAM; docker: the docker VM's allotment from `docker info`) prints a warning and proceeds — over-provisioning is allowed, never blocked. Minimums (1 CPU / 1 GiB) are enforced.

* Providers without resize support report that in the descriptor; attempting to set values errors clearly.

* For lima, values set while the VM runs take effect on the next stop+start (`mngr limit` says so); values set while stopped simply apply on next start. For docker, values apply live to the running container and persist across container stop/start.

### Minds settings page

* The per-workspace settings page gains a Resources section (placement decided during implementation) showing current CPU / memory with numeric integer inputs (min 1 CPU / 1 GiB). Hidden entirely for workspaces whose provider lacks resize support.

* Docker workspaces that have never been limited show "no limit" for each dimension rather than fake numbers.

* Typing a value above the physical ceiling shows an inline non-blocking warning (values come from the descriptor; works on macOS and Linux).

* Saving on a running lima workspace shows a confirmation dialog offering "Save & restart now" or "Save only — apply on next restart" (for when in-progress work shouldn't be interrupted). Restart-now runs: `mngr limit` → the existing host-scope restart operation, with its existing progress/status UI and operation-conflict guards (e.g. blocked during a running backup; concurrent resizes of the same workspace rejected the same way). Save-only leaves the values pending, shown via the same "pending restart" note + Restart-now button as CLI-set values.

* Saving on a running docker workspace applies live — no dialog, no restart.

* Saving while the workspace is already stopped saves silently; values apply on next start.

* If the page loads and configured ≠ actual (e.g. values were set via CLI without a restart), the section shows a "pending restart" note with a Restart-now button reusing the same restart flow.

* A reset-to-defaults button restores provider defaults (lima: 4 CPU / 4 GiB; docker: clear back to unlimited). If docker cannot clear a memory limit live, the descriptor-driven restart path handles it like a lima change.

* If a resize-triggered restart fails (e.g. VM won't boot with the new size), the host is left stopped and the error surfaces through the existing restart-operation/recovery UI — no automatic rollback.

### Agent-facing workspace API

* The resize endpoints join the existing agent-reachable workspace API (same `/api/v1/workspaces/...` blueprint, reached via the latchkey gateway proxy), so agents can request resource changes — e.g. an agent hitting memory pressure can ask to grow its own workspace.

* A new target-scoped permission verb `minds-workspaces-resize` gates resize writes, following the existing deny-all + user-approval flow; the grant dialog makes clear the permission may restart the workspace. It is deliberately separate from `minds-workspaces-update` because of that restart power.

* Reading capabilities + configured + actual values is covered by the existing `minds-workspaces-read` verb.

* The resize endpoint takes an explicit restart-to-apply flag: the settings UI sends it after its confirmation dialog; agents must request the restart explicitly too. A set-only call leaves values pending (surfaced as "pending restart" in the settings page).

### Minds create page

* The Create page's existing advanced view gains CPU and memory fields, pre-filled with the selected provider's defaults, plumbed through to workspace creation. Shown only for providers that support them.

### Reporting correctness

* `HostResources` stops lying: lima currently ignores start_args when recording resources, and docker returns a hardcoded 1 CPU / 1 GB placeholder. Both now reflect reality (configured values from the host record; docker "no limit" represented distinctly).

## Changes

* **mngr provider interface** (`libs/mngr`): new resize-capability descriptor data type and `get_resize_capabilities()` / `resize_host()` on `ProviderInstanceInterface`, with default not-supported implementations so all existing providers compile unchanged. `HostResources` gains the ability to represent "unlimited."

* **Lima provider** (`libs/mngr_lima`): store desired CPU/memory in the durable per-host record; apply them via `limactl edit` during `start_host`; read actuals from `limactl list --json`; report descriptor with machine-physical ceilings (psutil, already a dependency); fix resource recording at create time.

* **Docker provider** (`libs/mngr/providers/docker`): apply limits live via `docker update` and persist them; read actuals from `docker inspect`; report descriptor with ceilings from `docker info`; replace the placeholder `get_host_resources`; support clearing back to unlimited (restart-based if live clearing fails).

* **mngr CLI** (`cli/limit.py`): add `--cpus` / `--memory` flags, the no-flags read mode, over-provisioning warning, and updated help/docs (regenerate CLI docs via `scripts/make_cli_docs.py`).

* **Minds backend** (`apps/minds/desktop_client`): read path exposing capabilities + configured + actual for a workspace (shelling out to `mngr limit` read mode); resize endpoint invoking `mngr limit` and, when needed, the existing host-scope restart operation with its conflict guards; create flow plumbs CPU/memory through to creation.

* **Minds frontend**: Resources section in `WorkspaceSettings.jinja` + JS (inputs, warnings, confirm dialog, pending-restart note, reset button, restart progress reuse); CPU/memory fields in `Create.jinja`'s advanced view.

* **Agent-facing API plumbing** (`libs/mngr_latchkey` + `apps/minds`): register the resize write routes under a new `minds-workspaces-resize` verb in the workspace permission catalog (`workspace_permissions.json`) with matching grant-dialog metadata (`workspace_permissions.py`); include the read route under `minds-workspaces-read`; tests covering gateway permission gating for the new verb.

* **Tests**: unit tests across descriptor logic, CLI parsing, providers, and minds API handlers; docker integration test (create container → live resize → verify via inspect); lima release test (create VM → resize → restart → verify actuals); minds e2e test exercising the settings-page resize flow.

* **Changelog entries** for each touched project (`libs/mngr`, `libs/mngr_lima`, `apps/minds`).
