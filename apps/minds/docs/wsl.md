# Running minds under WSL2 on Windows (experimental)

minds has no packaged Windows build, but the full stack -- the Electron
desktop app, the backend, mngr, and Docker workspaces with the gVisor
runtime -- runs inside WSL2 on Windows, with the Electron window displayed
on the Windows desktop via WSLg. This page records the working recipe and
every deviation from the standard Linux flow, verified on Windows Server
2022 with WSL 2.7.10 and Ubuntu 24.04.

Status: experimental. Nothing in CI covers Windows; expect rough edges.

## Prerequisites and platform notes

- WSL2 (not WSL1): Docker and systemd require the real WSL2 kernel.
  On cloud VMs, WSL2 needs nested virtualization -- on AWS EC2 that means a
  `.metal` instance type; regular instances cannot run WSL2 at all. Note that
  Windows Server 2025 AMIs are UEFI-only and do not boot on the older metal
  instance types (m5zn/c5/z1d); Windows Server 2022 works.
- Install a current WSL from the MSI on
  [microsoft/WSL releases](https://github.com/microsoft/WSL/releases); the
  inbox `wsl.exe` on Windows Server 2022 is too old. Enable the
  `Microsoft-Windows-Subsystem-Linux` and `VirtualMachinePlatform` Windows
  features and reboot.
- WSLg (Linux GUI apps on the Windows desktop) is NOT in Windows Server's
  inbox WSL, but the MSI-installed WSL bundles a working WSLg even on
  Server 2022 -- this is what lets the Electron desktop client display.
  Verify with `ls /tmp/.X11-unix` (expect `X0`) inside the distro.

## In-distro setup (Ubuntu 24.04)

Follow the normal from-source flow (clone, `uv sync --all-packages`), plus:

1. Enable systemd and restart the distro (`/etc/wsl.conf`):

   ```ini
   [boot]
   systemd=true
   ```

2. Install Docker CE (get.docker.com) and gVisor. mngr requires runsc to be
   registered with `--overlay2=none` (its preflight error explains why); the
   plain `runsc install` default is rejected:

   ```bash
   sudo runsc install -- --overlay2=none
   sudo systemctl restart docker
   ```

   `docker run --runtime=runsc --rm alpine true` works out of the box under
   the WSL2 kernel.

3. Install node + the latchkey CLI. `minds run` hard-requires the `latchkey`
   binary; the Electron dev flow gets it from `pnpm install` in `apps/minds`,
   but nothing installs it for a headless run:

   ```bash
   npm install -g latchkey@<version pinned in apps/minds/package.json>
   ```

4. Build the web UI CSS once (otherwise every page is unstyled):

   ```bash
   cd apps/minds && pnpm install && pnpm run build:css
   ```

5. Install `just` (https://just.systems -- not packaged in Ubuntu 24.04) and
   Electron's GTK/NSS runtime libraries:

   ```bash
   sudo apt-get install -y libgtk-3-0t64 libnss3 libasound2t64 libatk-bridge2.0-0t64 libgbm1
   ```

6. Enable lingering so the systemd user session (and `/run/user/<uid>`,
   which `just` needs as its runtime dir) exists even though WSL's
   `wsl.exe -u <user>` sessions fail to start one (they print
   `Failed to start the systemd user session` -- harmless but real):

   ```bash
   sudo loginctl enable-linger <user>
   ```

## Launching the desktop app

CRITICAL: WSLg windows only appear in the Windows session that STARTED the
WSL instance. If WSL was first started headlessly (an SSH session, a boot-time
scheduled task), every GUI window renders into WSLg's compositor with no
RemoteApp bridge to any visible desktop -- apps run, `xdotool` sees their
windows, but nothing appears on screen. From your interactive (RDP or
console) session, run `wsl --shutdown` and start WSL again from that session;
verify the attachment with `tasklist /fi "IMAGENAME eq msrdc.exe"`, which
must show msrdc in YOUR session id. Re-run any keepalive task afterwards --
joining an already-running instance does not re-home WSLg, so the keepalive
keeps the instance alive without stealing the display.

The normal dev flow works once the display and runtime pieces above are in
place -- run it under tmux (see the daemonization note below) with the WSLg
X display:

```bash
tmux new-session -s minds-electron
export DISPLAY=:0
eval "$(uv run minds env activate <env>)"
just minds-start
```

The Electron window appears on the Windows desktop (over RDP too). The
Chromium sandbox works as-is: the WSL2 kernel does not carry Ubuntu's
`apparmor_restrict_unprivileged_userns` restriction that would otherwise
have to be disabled on native Ubuntu 24.04.

## Keeping it running

- WSL terminates the distro shortly after the last `wsl.exe` session exits,
  killing Docker, tmux, and minds. Keep a persistent session open. Headless
  (e.g. over SSH), use a scheduled task that holds one open from boot:

  ```powershell
  schtasks /create /tn WSL-Keepalive /sc onstart /ru <user> /rp <password> `
      /tr "C:\Windows\System32\wsl.exe -d Ubuntu-24.04 --exec sleep infinity"
  ```

- `minds run` watches its grandparent process and shuts down when the
  launching shell exits (that is how the Electron app avoids orphans), so
  `nohup`/`setsid` daemonization does not work. Run it inside tmux.

## After a WSL restart

`wsl --shutdown` (and anything else that stops the distro) kills the Docker
daemon and every workspace container with it. When bringing workspaces back,
use `mngr start` (e.g. `uv run mngr start --host <host>` under the activated
env), never a raw `docker start`: docker only resurrects the container and
its sshd, while the agent processes (tmux, supervisord, the system
interface) exist only in tmux sessions that mngr recreates. A raw
`docker start` leaves the workspace half-up -- reachable at the container
level but serving nothing -- until minds' health tracker marks it STUCK and
its recovery flow performs the proper restart a few minutes later.

## Assorted gotchas

- `wsl -d <distro> -u <user>` over Windows SSH prints
  `Failed to start the systemd user session` on stderr every time. Harmless
  (session-0 quirk), but it makes commands look failed.
- Windows sshd resolves `localhost` to `::1` first, while WSL's localhost
  relay listens on IPv4 only: SSH tunnels into the box must target
  `127.0.0.1`, not `localhost`.
- The first `apt-get install` on a fresh distro can fail with a transient
  dpkg error; retry once.
