# Running minds under WSL2 on Windows (experimental)

minds has no packaged Windows build, but the full stack -- the Electron
desktop app, the backend, mngr, and Docker workspaces -- runs inside WSL2 on
Windows, with the Electron window displayed on the Windows desktop via WSLg.
Verified on Windows Server 2022 (AWS metal) with WSL 2.7.10 and Ubuntu 24.04.

Status: experimental. Nothing in CI covers Windows; expect rough edges.

## Quick start

First get a WSL2 distro (skip whatever you already have):

- **Windows 11 desktop**: `wsl --install -d Ubuntu-24.04` in an elevated
  terminal, reboot if prompted. Requires hardware virtualization (enabled by
  default on almost all desktops).
- **Windows Server 2022 / AWS**: the inbox WSL is too old and lacks WSLg.
  Enable the `Microsoft-Windows-Subsystem-Linux` and `VirtualMachinePlatform`
  features, install the MSI from
  [microsoft/WSL releases](https://github.com/microsoft/WSL/releases), reboot,
  then `wsl --install -d Ubuntu-24.04`. On EC2, WSL2 needs nested
  virtualization: only `.metal` instance types work, and Windows Server 2025
  AMIs are UEFI-only and do not boot on the older metal types (m5zn/c5/z1d) --
  use Server 2022.

Then, inside the distro (from your interactive session -- a Windows Terminal
tab, not SSH; see the session-affinity gotcha below):

```bash
curl -fsSL https://raw.githubusercontent.com/imbue-ai/mngr/main/apps/minds/scripts/install-linux.sh | bash
```

This is the same Linux installer every platform uses
([dev-setup.md](./dev-setup.md)); it detects WSL from `/proc/version` and adds
the WSL-specific steps. It narrates each step, is idempotent (re-run it to
update), and ends by launching the app and dropping a "Mind (WSL)" shortcut on
the Windows desktop for next time. Flags (pass via `bash -s -- <flags>`):

- `--version REF` -- mngr ref to install (`latest` for the newest `minds-v*`
  tag; a fresh clone without it lands on `main`, and an existing checkout is
  left as it is)
- `--install-dir DIR` (default `~/mngr`), `--no-launch`, `--skip-docker`
- `--yes` runs every privileged step without prompting; `--non-interactive`
  never prompts and instead prints the exact command for any privileged step
  that still needs doing

It installs, skipping anything already present: apt basics + Electron's
GTK/NSS libraries, Docker CE, systemd lingering (keeps the app's background
daemons alive between sessions), uv, nvm + the pinned node, pnpm, the mngr
checkout, `uv sync`, and `pnpm install`. The launcher it writes
(`~/.local/bin/minds-desktop`) runs the app from source against production.

It refuses clearly on: WSL1, missing systemd (it writes `/etc/wsl.conf` and
asks you to `wsl --shutdown` and re-run), non-apt distros, <20GB free disk (5GB with `--skip-docker`), an
install dir under `/mnt/` (Windows-filesystem line endings and IO would bite),
and **Docker Desktop WSL integration** -- minds has not been verified against
the Docker Desktop daemon (follow-up task; for now disable the integration for
this distro or use a separate distro so Docker CE can be installed).

## Container runtime: runc by default

New workspaces default to Docker's standard `runc` runtime on every platform.
Under WSL the utility VM is the isolation boundary between containers and
Windows -- the same posture as the Docker VM on macOS. Note what that does NOT
cover: a container escape would land in the WSL distro itself, so keep secrets
you care about out of the distro or opt into gVisor.

gVisor remains a per-create opt-in (advanced settings -> runsc) and works
under WSL2 once registered the way mngr requires:

```bash
sudo runsc install -- --overlay2=none
sudo systemctl restart docker
```

(A plain `runsc install` is rejected by mngr's preflight -- its error message
explains why: gVisor's default overlay discards root-filesystem writes.)

## After a WSL restart

`wsl --shutdown` (and anything else that stops the distro) kills the Docker
daemon and every workspace container with it. When bringing workspaces back,
use `mngr start` (e.g. `MNGR_HOST_DIR=~/.minds/mngr MNGR_PREFIX=minds- uv run mngr start --host <host>`,
the same host dir and prefix the app runs with), never a raw `docker start`: docker only resurrects the container and
its sshd, while the agent processes (tmux, supervisord, the system
interface) exist only in tmux sessions that mngr recreates. A raw
`docker start` leaves the workspace half-up -- reachable at the container
level but serving nothing -- until minds' health tracker marks it STUCK and
its recovery flow runs that `mngr start` for you a few minutes later.

## Gotchas (mostly for headless / remote setups)

- CRITICAL -- WSLg session affinity: WSLg windows only appear in the Windows
  session that STARTED the WSL instance. If WSL was first started headlessly
  (an SSH session, a boot-time scheduled task), every GUI window renders into
  WSLg's compositor with no RemoteApp bridge to any visible desktop -- apps
  run, `xdotool` sees their windows, but nothing appears on screen. From your
  interactive (RDP or console) session, run `wsl --shutdown` and start WSL
  again from that session; verify with
  `tasklist /fi "IMAGENAME eq msrdc.exe"`, which must show msrdc in YOUR
  session id. Re-run any keepalive task afterwards -- joining an
  already-running instance does not re-home WSLg.
- WSL terminates the distro shortly after the last `wsl.exe` session exits,
  killing Docker, tmux, and minds. Interactively this doesn't bite (the app
  window keeps the instance alive); for unattended boxes, hold a session open
  with a scheduled task:

  ```powershell
  schtasks /create /tn WSL-Keepalive /sc onstart /ru <user> /rp <password> `
      /tr "C:\Windows\System32\wsl.exe -d Ubuntu-24.04 --exec sleep infinity"
  ```

- `minds run` watches its grandparent process and shuts down when the
  launching shell exits (that is how the Electron app avoids orphans), so
  `nohup`/`setsid` daemonization does not work. The Electron app owns its
  backend, so this only matters for running the bare backend; use tmux then.
- `wsl -d <distro> -u <user>` over Windows SSH prints
  `Failed to start the systemd user session` on stderr every time. Harmless
  (session-0 quirk), but it makes commands look failed.
- Windows sshd resolves `localhost` to `::1` first, while WSL's localhost
  relay listens on IPv4 only: SSH tunnels into the box must target
  `127.0.0.1`, not `localhost`.
- The first `apt-get install` on a fresh distro can fail with a transient
  dpkg error; retry once.
