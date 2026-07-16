# Running minds under WSL2 on Windows (experimental)

minds has no packaged Windows build, but the full stack (backend, mngr, Docker
workspaces with the gVisor runtime) runs inside WSL2 on Windows. This page
records the working recipe and every deviation from the standard Linux flow,
verified on Windows Server 2022 with WSL 2.7.10 and Ubuntu 24.04.

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
- Windows Server has no WSLg, so the Electron desktop client cannot display
  from WSL. Run the backend headless (`uv run minds run --no-browser`) and use
  a Windows browser at `http://localhost:8420` instead -- WSL's localhost
  forwarding makes the port reachable from Windows.

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

## Assorted gotchas

- `wsl -d <distro> -u <user>` over Windows SSH prints
  `Failed to start the systemd user session` on stderr every time. Harmless
  (session-0 quirk), but it makes commands look failed.
- Windows sshd resolves `localhost` to `::1` first, while WSL's localhost
  relay listens on IPv4 only: SSH tunnels into the box must target
  `127.0.0.1`, not `localhost`.
- The first `apt-get install` on a fresh distro can fail with a transient
  dpkg error; retry once.
