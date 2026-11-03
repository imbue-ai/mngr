# Testing Imbue Studio on Linux from a Mac

A Lima VM runs the real Linux app against a real Linux filesystem, which is what
you need to check anything the path resolver, the data migration or the Electron
shell does differently there. There is no packaged Linux arm64 build (the
artifacts are x86_64 only), so this is the same dev-mode launch
[raspberry-pi.md](./raspberry-pi.md) describes, in a VM instead of on a Pi.

## Bring-up

```bash
limactl create --name=linux-test --vm-type=vz --cpus=4 --memory=8 --disk=60 template:ubuntu-24.04
limactl start linux-test
```

Do **not** pass `--video`: Lima's host agent segfaults on `vz`, so the display
comes from VNC instead.

```bash
limactl shell linux-test
sudo apt-get update && sudo apt-get install -y xvfb x11vnc xfce4 xfce4-terminal dbus-x11 xdotool
Xvfb :99 -screen 0 1600x1000x24 &
DISPLAY=:99 dbus-launch --exit-with-session xfce4-session &
x11vnc -storepasswd <password> ~/.vnc/passwd
x11vnc -display :99 -forever -shared -rfbauth ~/.vnc/passwd -rfbport 5900 &
```

A password is required: macOS Screen Sharing refuses a no-auth VNC server, so
`-nopw` connects and then prompts forever. Install a full desktop rather than a
bare window manager, or there is no way to open a terminal. Lima forwards 5900
on its own; connect with `open vnc://localhost:5900`.

## The code under test

Clone the branch from the read-only worktree mount inside the VM, which needs no
credentials and leaves your checkout untouched:

```bash
git clone --branch <branch> /Users/<you>/path/to/worktree ~/mngr
cd ~/mngr && apps/minds/scripts/install-linux.sh --yes --skip-docker --no-launch
```

Dev-mode Electron ships its own sandbox helper unprivileged, so fix it once:

```bash
S=$(find ~/mngr/apps/minds/node_modules/.pnpm -name chrome-sandbox -path '*electron*' | head -1)
sudo chown root:root "$S" && sudo chmod 4755 "$S"
```

## Launching

```bash
DISPLAY=:99 ~/.local/bin/minds-desktop
```

Use that launcher rather than `pnpm start`: it selects the pinned Node and pnpm,
which a non-login shell does not have on `PATH` (the build then fails spawning
`uv`). The app writes to the roots in
[desktop-app.md](./desktop-app.md#data-directory); `~/.config/Imbue Studio`
staying absent is what shows the `userData` override held. That directory takes
`productName`, the way Electron names every path it derives from app identity
off macOS; only the data roots take `imbue-studio`.

## Workspaces

Linux creates workspaces in Docker, not Lima, so nothing is nested. The
`--skip-docker` above left this out; install it the way
`apps/minds/scripts/install-linux.sh` does, since `docker-ce` comes from
Docker's own apt repository rather than Ubuntu's and mngr needs an Engine newer
than `docker.io` there:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

A running app cannot pick up a new group, and neither can an existing SSH
session, so start it with the group rather than re-logging in:

```bash
setsid sg docker -c "env DISPLAY=:99 $HOME/.local/bin/minds-desktop"
```

On arm64 the workspace image is built locally from `default-workspace-template`
at the pinned `minds-v*` tag, so the first create takes upwards of ten minutes,
and may need `MNGR__PROVIDERS__DOCKER__BUILD_TIMEOUT_SECONDS` raised past its
600s default. The runtime is `runc`; gVisor is a per-create opt-in and is not
registered here.

## Signing in

Sign-in opens a browser, and a server image has none, so `xdg-open` fails with
"Failed to execute default Web Browser". Ubuntu's `firefox` is a snap, which
does not work well here; take the `.deb` from Mozilla's apt repo and make it the
default:

```bash
curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg | sudo tee /etc/apt/keyrings/packages.mozilla.org.asc >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" | sudo tee /etc/apt/sources.list.d/mozilla.list
sudo apt-get update && sudo apt-get install -y firefox
DISPLAY=:99 xdg-settings set default-web-browser firefox.desktop
```

## Exercising a migration

To watch a first launch move an existing install, seed the legacy root before
launching, with a file per destination role and the key at the mode latchkey
requires:

```bash
mkdir -p ~/.minds/{auth/sessions,latchkey,logs,.uv-cache,template-cache}
echo canary > ~/.minds/canary.txt
echo key > ~/.minds/latchkey/encryption_key && chmod 600 ~/.minds/latchkey/encryption_key
chmod 700 ~/.minds/latchkey
```

Seeding with a looser mode makes the backend exit 1 on `LatchkeyError`, which
looks like a migration failure and is not one.
