# Plan: sshd + agent restart robustness for minds templates

## Overview

- Make sshd survive out-of-band container restarts by having mngr's container entrypoint self-heal sshd on every (re)start, instead of relying solely on `mngr start` to re-exec it. mngr owns container PID 1 (`tail -f /dev/null`), so this cannot be fixed from the FCT Dockerfile alone.
- Keep mngr's existing explicit sshd start, but make it idempotent so the entrypoint's self-heal and mngr's start never collide or double-start.
- Ensure containers actually come back after a daemon/host restart (`--restart=unless-stopped`), and that the minds `system-services` agent is automatically relaunched even when the desktop app is not running, via a VM-boot hook.
- Keep the change layered correctly: generic, provider-wide mechanisms live in `mngr`; the minds-specific "always start `system-services`" policy is declared from FCT configuration, not hardcoded in Python.
- Make `mngr start` safe to run concurrently (desktop-remote start vs in-VM/in-container boot-hook start) by adding host-level locking on the start path and upgrading the remote cooperative lock to a real flock-over-SSH.

## Expected behavior

- After a manual `docker restart`, a docker daemon restart, or an outer VM/host reboot, sshd inside the container comes back automatically (using the persisted host key), so mngr/minds can reach the host without a host-key mismatch.
- The minds `system-services` agent is automatically restarted after such events, even with the minds desktop app closed:
  - local lima: a VM systemd unit runs `mngr start system-services` directly in the VM.
  - imbue_cloud: a VM systemd unit `docker start`s the agent container, then runs `mngr start system-services` inside it.
- Only the `system-services` agent is auto-started by the boot hook; it is always started and no other agents are touched.
- The minds desktop client's existing recovery (reactive, scoped to the viewed workspace) is unchanged and continues to cover crashed/unknown agents and the foreground case.
- Running `mngr start` while another start is already in progress on the same host blocks until the first completes, then no-ops if the agent is already running (wrap with `timeout` for a deadline). The desktop-remote start and the boot-hook start can no longer race into duplicate sshd processes or duplicate tmux sessions.
- mngr's explicit sshd start becomes a no-op when sshd is already running (e.g. already brought up by the self-heal entrypoint), so there is no confusing double-start.
- Local minds DOCKER mode: the container and sshd return after a restart, but the agent is relaunched only by the desktop client (docker mode is mainly for debugging); no container-level agent auto-start.
- Behavior on first create is unchanged: the entrypoint's sshd self-heal is a no-op until mngr has provisioned a host key, after which mngr starts sshd as it does today.
- Intentionally stopped agents other than `system-services` are not resurrected by the boot hook; `system-services` is always brought back on boot by design.

## Changes

### Generic mngr (provider-wide)

- Self-healing container entrypoint: on container (re)start, start sshd if a host key already exists, then idle — for both the in-tree `docker` provider and the VPS `DockerRealizer` (covers imbue_cloud and all VM-backed containers). Preserve the existing clean-SIGTERM-on-stop behavior.
- Idempotency guard on mngr's in-container sshd start so it is a true no-op when sshd is already running.
- Start-path locking: take the existing host cooperative lock around the `mngr start` host-start + agent-start steps (matching how `create`/`gc` already lock), and make agent (tmux session) launch idempotent so concurrent starts cannot double-launch.
- Upgrade the remote-host cooperative lock from a best-effort lock-file write to a real flock-over-SSH, so local and remote `mngr start` callers mutually exclude on the same lock; the lock blocks indefinitely and re-checks state after acquisition.
- New generic, commands-only create-template hook that runs once on host create on the outer host (via `host.outer_host()`), with a sensible policy when no outer host exists. Add the corresponding CLI flag and settings/template field.

### FCT (`forever-claude-template`) repo — in a `.external_worktrees/` worktree on the matching branch

- Add `--restart=unless-stopped` to the docker create-template start args (local docker mode) and to the imbue_cloud inner-container create-template start args.
- local lima: use the existing in-VM `extra_provision_command` to install + enable a systemd unit that runs `mngr start system-services` on VM boot.
- imbue_cloud: use the new outer-provision hook to install + enable a systemd unit on the outer VM that, on boot, `docker start`s the agent container and runs `mngr start system-services` inside it.

### Out of scope / deferred

- Full implementation of the generic `start_on_boot` agent flag.
- Any change to the minds desktop client recovery logic.
- Container-level agent auto-start for local docker mode.

### Changelog / tests

- Add per-project changelog entries for every touched project (`libs/mngr`, `libs/mngr_imbue_cloud`, and the FCT repo), plus any provider lib touched (e.g. `libs/mngr_vps`).
- Cover with tests: entrypoint self-heal restores sshd after a restart; mngr's sshd start is idempotent; concurrent `mngr start` is safe; the new outer-provision hook runs on the outer once at create.
