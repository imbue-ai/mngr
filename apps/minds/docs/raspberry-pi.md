# Running minds on a Raspberry Pi

A 64-bit Raspberry Pi 5 can host the production-tier minds desktop client, run
from source, as an always-on box you reach over VNC. This is useful when your
main machine is the one you *develop* minds on: the Pi runs the real thing,
against production, with its own `~/.minds/` data root, and never collides with
a dev checkout.

There is no packaged Linux arm64 build (the packaged Linux artifacts are x86_64
only; see [desktop-app.md](./desktop-app.md)), so the Pi runs the same dev-mode
launch a developer uses (`pnpm start`, i.e.
`electron .` against the monorepo venv), which targets production by default.
`scripts/install-linux.sh --raspberry-pi` does the whole bring-up -- the same
Linux installer every platform uses ([dev-setup.md](./dev-setup.md)), with the
Pi-specific steps switched on; this page explains what it does and why.

## What you need

- Raspberry Pi 5 (or another arm64 Pi with enough RAM; workspace containers
  are built on the Pi itself) running Raspberry Pi OS bookworm, 64-bit, with
  the desktop packages installed (the "Raspberry Pi OS with desktop" image),
  ssh enabled, and passwordless sudo for your user (the Pi OS default).
- A checkout of this repo on the Pi: `git clone https://github.com/imbue-ai/mngr ~/mngr`
  (or let the installer clone it by running it curl-piped with
  `--raspberry-pi`, as in [dev-setup.md](./dev-setup.md)). To run a branch
  that is not on the public repo, ship it as a git bundle from your machine:

  ```bash
  git bundle create /tmp/mngr.bundle <branch>
  scp /tmp/mngr.bundle <user>@<pi>:/tmp/mngr.bundle
  ssh <user>@<pi> 'git clone -b <branch> /tmp/mngr.bundle ~/mngr && rm /tmp/mngr.bundle'
  ```

  Bundle the branch itself, not `HEAD`: a `HEAD`-only bundle carries no branch
  ref, so `git clone -b <branch>` cannot find it.

  To update later, bundle only the new commits and fast-forward the Pi's
  checkout from it, then re-run the setup script (it re-syncs the venv and
  rebuilds the SPA):

  ```bash
  git bundle create /tmp/inc.bundle <branch> ^<sha-the-pi-has>
  scp /tmp/inc.bundle <user>@<pi>:/tmp/inc.bundle
  ssh <user>@<pi> 'cd ~/mngr && git pull --ff-only /tmp/inc.bundle <branch> && rm /tmp/inc.bundle'
  ```

## Bring-up

```bash
ssh -t <user>@<pi> '~/mngr/apps/minds/scripts/install-linux.sh --raspberry-pi --no-launch'
ssh <user>@<pi> sudo reboot
```

Run from inside the checkout, the installer uses it and clones nothing. Each
step that needs sudo shows its command and asks first; pass `--yes` to run
them all unprompted. The script is idempotent. It:

1. Installs Docker CE via get.docker.com (Debian's `docker.io` is 20.10;
   mngr's isolated host volumes need Engine >= 25), plus `tmux`, `jq`, and
   `grim` (Wayland screenshots), and adds your user to the `docker` group.
2. Installs gVisor's `runsc` at the release `mngr_vps` pins and registers it
   with `--overlay2=none`, exactly as the cloud hosts do. Workspaces default
   to `runc`; `runsc` is a per-create opt-in under the form's advanced
   settings, and this makes it available on the Pi.
3. Fixes two Pi kernel defaults gVisor cannot live with: the firmware boots
   with `cgroup_disable=memory` (so `cgroup_enable=memory` is added to
   `cmdline.txt`), and the Pi 5 default `kernel_2712.img` uses 16K pages (so
   `config.txt` selects the 4K-page `kernel8.img`). Both need one reboot.
4. Installs uv, nvm with the Node version `apps/minds/.nvmrc` pins, and the
   pnpm version `package.json` pins.
5. Boots to the desktop with autologin (Electron needs a session to draw into)
   and enables `wayvnc`, Raspberry Pi OS's VNC server for its Wayland (labwc)
   desktop. RealVNC's X11 service is disabled; it cannot capture a Wayland
   session.
6. Builds the checkout: `uv sync --all-packages`, `pnpm install` in
   `apps/minds` (Playwright browser download skipped), the arm64 bundled
   binaries via `ensure-binaries.js`, and the SPA bundle.
7. Installs `~/.local/bin/minds-desktop`, a menu entry, and an autostart entry,
   so the client comes up with the desktop after every boot.

## Using it

Connect any VNC client (RealVNC Viewer, TigerVNC, macOS Screen Sharing) to
`<pi>:5900`. wayvnc authenticates with your Pi username and password over TLS,
and listens on every interface, so keep the Pi on a network you trust or
tunnel it (`ssh -N -L 5901:127.0.0.1:5900 <user>@<pi>` and connect to
`localhost:5901`).

The minds window is already open (or launch it from the menu). First launch
walks through the normal sign-in; create workspaces in DOCKER mode. The
workspace image is built on the Pi from the public
`default-workspace-template` at the pinned `minds-v*` tag, so the first
create takes a while.

The launcher runs against production, so data lives in `~/.minds/` and
logs in `~/.minds/logs/`. It runs `pnpm start`, whose `prestart` hook
re-provisions binaries and rebuilds the SPA on every launch, and raises mngr's
Docker build timeout to an hour (`MNGR__PROVIDERS__DOCKER__BUILD_TIMEOUT_SECONDS`),
since the first workspace image build on a Pi runs past the 600s default.

## arm64 specifics

`scripts/download-binaries.js` provisions the bundled binaries per
platform/arch. `linux/arm64` maps to the `aarch64` release assets of uv,
restic, lima, desync, the dugite-native git payload, and datalib's
Chrome-impersonating curl (all pinned and SHA256-verified, like the shipped
platforms). Lima mode is not exercised on the Pi (it would run the VM under
host QEMU); DOCKER is the supported launch mode there.
