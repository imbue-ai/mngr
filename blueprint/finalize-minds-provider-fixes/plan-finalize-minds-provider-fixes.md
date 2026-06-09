# Finalize minds workspace-provider create fixes

Final cleanup pass on the standardized minds workspace-provider stack: drop runsc from lima, pin lima's docker the same way as VPS, make backup-secret injection robust without blocking create, replace "preferred region" with an explicit per-provider region, and stop showing destroyed hosts.

## Overview

- **Lima drops runsc**: lima runs docker directly (`runc`), not gVisor. runsc stays in place only for docker/vultr/ovh. This is a config-only change in the forever-claude-template's `.mngr/settings.toml`; the dead lima runsc install code is left untouched.
- **Lima pins docker like VPS**: lima switches from Debian's unpinned `docker.io` to the official Docker CE apt repo at the same pinned version as the remote VPS providers, installed the same way (apt, `--allow-downgrades`, same package set). The pinned version string is hardcoded independently in `mngr_lima` (no cross-lib coupling).
- **Backups stop racing/blocking create**: restic backup setup is gated on workspace readiness, retried for up to ~5 minutes with each `mngr exec` bounded to ~60s, on the existing detached thread so it never blocks create. The litellm key path and `mngr create` are left unchanged.
- **Region becomes explicit and per-provider**: the convoluted "preferred region" geolocation machinery is removed everywhere (minds, the once-per-hour refresh, and the `preferred_region` lease param in mngr_imbue_cloud + connector). Region is an explicit advanced-settings dropdown that defaults to the last-used value stored per provider in `~/.minds/config.toml`, then a background-detected geo fallback, then a hardcoded default.
- **Destroyed hosts disappear from the UI**: `host_state` is threaded from discovery into the minds front end so DESTROYED hosts are hidden from every workspace surface and destroy no longer reports a bogus "failed".

## Expected behavior

### Lima runsc + docker pinning
- Creating a minds workspace via the lima provider no longer downloads, installs, or configures gVisor/runsc inside the lima VM.
- The agent container in lima runs under the default `runc` runtime; the runsc-specific container workarounds (`--workdir=/`, `--security-opt=no-new-privileges`) are gone for lima.
- docker/vultr/ovh providers continue to run agent containers under runsc, unchanged.
- The lima VM installs the same pinned docker version as the remote VPS providers (`docker-ce` etc. at `5:29.5.1-1~debian.12~bookworm`), from the official Docker CE apt repo.

### Backup secret injection
- The litellm key continues to ride into the workspace via `mngr create` (unchanged); `/welcome` and the onboarding message are unaffected in ordering.
- restic backup configuration only attempts injection once the workspace host is actually ready, so slow lima VMs no longer cause the restic env to silently miss the VM.
- If injection fails transiently, minds keeps retrying for up to ~5 minutes; each attempt is bounded (~60s) so a single hung `mngr exec` can't stall the loop.
- Failed attempts are logged at debug level only; the user is notified once, only if the whole ~5-minute budget is exhausted.
- Backup setup never blocks the create call or the redirect to the new workspace.

### Region selection
- The create form always shows an explicit "Region" control under advanced settings for providers that support it (imbue_cloud, vultr); it is hidden for providers that don't (local/docker/lima/modal).
- imbue_cloud offers a dropdown of {US-EAST-VA, US-WEST-OR}; vultr offers the full Vultr region list. Both allow a free-text fallback.
- The control defaults to the last-used region for that provider (from `~/.minds/config.toml`); if there's no stored value, it falls back to a background geo-detected region; if geo hasn't returned (or the stored value isn't a known region), it falls back to a hardcoded default (US-EAST-VA for imbue_cloud, `ewr` for vultr).
- On a successful create, the chosen region is written back to that provider's section in `~/.minds/config.toml` so it becomes the next default.
- Geolocation is fetched once in the background at startup (no more once-per-hour refresh); the detected region is logged for debugging and used only as an in-memory form default, never persisted on its own.
- If a region has no capacity, the create error surfaced to the user clearly indicates there are no machines available in that region (so they know to try another).

### Destroyed hosts
- A workspace whose host is DESTROYED drops off the active workspace list (Landing and every other surface derived from the known-workspace list).
- Destroying a workspace reports DONE rather than a false "failed" once the host is gone, even during mngr's destroyed-host persistence window.
- A just-destroyed host is recognized promptly via the discovery delta event, not only on the next full snapshot.
- Destroyed-host details remain reachable via the existing on-demand host-state path (e.g. for any future restore view).

## Changes

### Task 1 — Disable runsc in lima (config-only, in the FCT template)
- In `.external_worktrees/forever-claude-template/.mngr/settings.toml`, lima provider section: set `install_gvisor_runtime = false`, set `docker_runtime = "runc"`, and remove the runsc-specific `default_container_run_args` workarounds.
- Commit this change in the FCT worktree on the same branch.
- Leave the `_GVISOR_RUNSC_INSTALL_BLOCK` and its `install_gvisor_runtime` guard in `mngr_lima/lima_yaml.py` in place (still reachable if explicitly enabled); no code removal.
- Keep runsc enabled for docker/vultr/ovh.

### Task 2 — Pin lima's docker version (via apt, like VPS)
- Change lima's provisioning so docker is installed from the official Docker CE apt repo instead of Debian's `docker.io`.
- Mirror the VPS install exactly: same packages (`docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-buildx-plugin`, `docker-compose-plugin`), same pinned apt version, with `--allow-downgrades`.
- Hardcode the same pinned version string in `mngr_lima` independently (no dependency on `mngr_vps_docker`); the lima VM is Debian 12 bookworm, so the existing pin is valid.

### Task 3 — Robust backup-secret injection (no blocking, no race)
- Gate restic backup configuration on workspace readiness before attempting `mngr exec` injection (reuse the existing readiness signal).
- Wrap the whole backup-provisioning operation in a retry loop with a ~5-minute overall budget, running on the existing detached thread.
- Bound each restic `mngr exec` to ~60s.
- Log each failed attempt at debug level; surface a single user notification only if the budget is exhausted.
- Leave the litellm key path and `mngr create` (including its current no-timeout behavior) unchanged.

### Task 4 — Explicit per-provider region
- Add an explicit region control to the create form's advanced settings, shown only for region-supporting providers (imbue_cloud, vultr); thread the selected value through the create request/handler into the `mngr create` invocation.
- Pass region as `-b region=<value>` for imbue_cloud and `--vps-region=<value>` for vultr.
- Add per-provider region storage under a `[providers.<provider_name>]` table in `~/.minds/config.toml` (read for the default, written back only on a successful create).
- Implement the form default precedence: stored config value (if a known region) → background geo-detected region → hardcoded default (US-EAST-VA / `ewr`).
- Replace the geolocation machinery: remove the once-per-hour refresh, fetch geo once at startup in the background, log the detected region, and use it only as an in-memory fallback.
- Provide a hardcoded geo→region table for the full Vultr region list (lat/long, nearest-match), alongside the existing imbue_cloud/OVH-US mapping.
- Remove "preferred region" entirely: minds usage and config methods, `region_preference.py`'s throttle/refresh, and the `preferred_region` lease param in `mngr_imbue_cloud` and the remote connector service's lease endpoint.
- Ensure the out-of-capacity error from `mngr create` is surfaced comprehensibly and mentions region capacity when that's the cause (no elaborate per-provider error mapping).

### Task 5 — Hide DESTROYED hosts from the front end
- Thread `host_state` (per `host_id`) from discovery into the minds front end: capture it from the full snapshot's `hosts`, and keep it fresh via the delta events (`HostDiscoveryEvent`, and especially `HostDestroyedEvent` so a just-destroyed host is recognized immediately).
- Carry `host_state` (or a host_id→host_state mapping) into the resolver snapshot (`ParsedAgentsResult`) and store it in the resolver.
- Expose host state from the resolver and let each consumer decide (no central filtering): the Landing list and all other consumers of `list_known_workspace_ids()` exclude DESTROYED hosts; `read_destroying` treats a DESTROYED host as gone (returns DONE, not FAILED).
- Audit every consumer of `list_known_workspace_ids()` and apply the DESTROYED exclusion consistently.
- Leave the existing on-demand host-state path (host-health probe via `mngr list`) intact so destroyed-host info stays reachable.
- Update `apps/minds/docs/destroyed-host-still-listed.md` to reflect the implemented design.

### Changelog
- Add per-project changelog entries for every project touched: `apps/minds`, `libs/mngr_lima`, `libs/mngr_imbue_cloud`, the connector project, and `dev/` if root-level files change. (`mngr_vps_docker` only if its code is actually modified — Task 2 hardcodes in lima, so likely not.)
