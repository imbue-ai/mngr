#!/bin/bash
# Idempotent bring-up of the minds desktop app inside WSL2.
#
# Run inside an Ubuntu/Debian WSL2 distro (see apps/minds/docs/wsl.md for how
# to get one on Windows desktop, Windows Server, or an AWS metal instance):
#
#   curl -fsSL https://raw.githubusercontent.com/imbue-ai/mngr/main/apps/minds/scripts/install-wsl.sh | bash
#
# What it does (each step narrated, skipped when already satisfied):
#   1. Preflight: WSL2, systemd, apt, disk space, no Docker Desktop conflict.
#   2. apt packages (git/tmux/jq/..., Electron's GTK/NSS libraries).
#   3. Docker CE (docker.com convenience script) + docker group membership.
#   4. loginctl lingering (systemd user session for the `just` runtime dir).
#   5. uv and just into ~/.local/bin; nvm + the pinned node; pnpm; latchkey.
#   6. Clone/update the mngr repo (latest minds-v* tag by default).
#   7. uv sync + pnpm install.
#   8. A launcher (~/.local/bin/minds-wsl-start) and a Windows desktop
#      shortcut, then starts the app (unless --no-launch).
#
# Under WSL the launcher defaults new workspaces to Docker's standard `runc`
# runtime (via MINDS_DOCKER_RUNTIME_DEFAULT): the WSL2 utility VM is the
# isolation boundary between containers and Windows, the same posture as the
# Docker VM on macOS. gVisor (runsc) remains available as an explicit opt-in;
# see docs/wsl.md.
#
# Flags:
#   --env NAME         minds env to activate (default: production)
#   --version REF      mngr git ref to install (default: latest minds-v* tag;
#                      pass `main` for the development tip)
#   --dev              contributor layout: default --version to main, clone
#                      default-workspace-template locally, launch via
#                      `just minds-start` (which syncs vendor/mngr)
#   --install-dir DIR  where to clone mngr (default: ~/mngr)
#   --no-launch        set everything up but do not start the app
set -euo pipefail

MNGR_REPO_URL="https://github.com/imbue-ai/mngr.git"
DWT_REPO_URL="https://github.com/imbue-ai/default-workspace-template.git"

ENV_NAME="production"
VERSION=""
IS_DEV=0
INSTALL_DIR="$HOME/mngr"
IS_LAUNCH=1

while [ $# -gt 0 ]; do
    case "$1" in
        --env) ENV_NAME="$2"; shift 2 ;;
        --version) VERSION="$2"; shift 2 ;;
        --dev) IS_DEV=1; shift ;;
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --no-launch) IS_LAUNCH=0; shift ;;
        *) echo "error: unknown flag: $1" >&2; exit 2 ;;
    esac
done

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
skip() { printf '\033[2m    (already done: %s)\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
step "Preflight checks"

if [ "$(id -u)" = "0" ]; then
    die "run this as your normal user, not root -- the script uses sudo where needed"
fi

if ! grep -qi microsoft /proc/version 2>/dev/null; then
    die "this does not look like WSL. This script sets up minds inside a WSL2 distro; see apps/minds/docs/wsl.md"
fi
if ! uname -r | grep -qi 'WSL2\|microsoft-standard'; then
    die "this looks like WSL1. minds needs WSL2 (Docker and systemd require the real kernel).
Fix from Windows:  wsl --set-version ${WSL_DISTRO_NAME:-<distro>} 2"
fi

if ! command -v apt-get >/dev/null 2>&1; then
    die "this script supports Debian/Ubuntu distros (apt-get not found). Install Ubuntu 24.04:  wsl --install -d Ubuntu-24.04"
fi

if [ "$(ps -p 1 -o comm=)" != "systemd" ]; then
    if [ ! -e /etc/wsl.conf ]; then
        step "Enabling systemd in /etc/wsl.conf (required for Docker)"
        printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf >/dev/null
        die "systemd enabled, but the distro must restart for it to take effect.
From Windows run:  wsl --shutdown
Then re-run this script."
    fi
    die "PID 1 is not systemd and /etc/wsl.conf already exists.
Add the following to /etc/wsl.conf, then run 'wsl --shutdown' from Windows and re-run:
[boot]
systemd=true"
fi

available_kb=$(df -Pk "$HOME" | awk 'NR==2 {print $4}')
if [ "$available_kb" -lt $((20 * 1024 * 1024)) ]; then
    die "less than 20GB free on $HOME ($((available_kb / 1024 / 1024))GB available). Free up disk space first (from Windows, the WSL virtual disk grows on demand up to its size cap)."
fi

case "$INSTALL_DIR" in
    /mnt/*) die "--install-dir must be inside the Linux filesystem (e.g. ~/mngr), not under /mnt/ -- the Windows filesystem breaks git line endings and is drastically slower" ;;
    *) : ;;
esac

if command -v docker >/dev/null 2>&1; then
    docker_path=$(readlink -f "$(command -v docker)")
    case "$docker_path" in
        *docker-desktop*) die "this distro's docker comes from Docker Desktop's WSL integration, which minds has not been verified against (and which cannot register alternative runtimes).
Either disable Docker Desktop's integration for this distro (Docker Desktop -> Settings -> Resources -> WSL integration) so this script can install Docker CE, or use a separate distro.
(Follow-up task: verify minds against the Docker Desktop daemon -- see apps/minds/docs/wsl.md.)" ;;
        *) : ;;
    esac
fi

step "Requesting sudo access (used for apt, docker, and lingering setup)"
sudo -v

# ---------------------------------------------------------------- apt packages
step "Installing apt packages (base tools + Electron runtime libraries)"

# Installs the first available candidate for each |-separated group, so Ubuntu
# 24.04's t64-suffixed library names and Debian's plain ones both resolve.
apt_packages=(
    git tmux jq curl rsync ca-certificates build-essential openssh-client
    "libgtk-3-0t64|libgtk-3-0"
    libnss3
    "libasound2t64|libasound2"
    "libatk-bridge2.0-0t64|libatk-bridge2.0-0"
    libgbm1
)
resolved_packages=()
sudo apt-get update -q
for group in "${apt_packages[@]}"; do
    chosen=""
    IFS='|' read -ra candidates <<< "$group"
    for candidate in "${candidates[@]}"; do
        if apt-cache show "$candidate" >/dev/null 2>&1; then
            chosen="$candidate"
            break
        fi
    done
    if [ -z "$chosen" ]; then
        die "none of the package candidates '$group' exist in this distro's apt archive"
    fi
    resolved_packages+=("$chosen")
done
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${resolved_packages[@]}"

# ---------------------------------------------------------------- docker
if command -v docker >/dev/null 2>&1; then
    skip "docker is installed ($(docker --version 2>/dev/null || sudo docker --version))"
else
    step "Installing Docker CE (via get.docker.com)"
    curl -fsSL https://get.docker.com | sudo sh
fi
sudo systemctl enable --now docker

if id -nG "$USER" | grep -qw docker; then
    skip "$USER is in the docker group"
else
    step "Adding $USER to the docker group"
    sudo usermod -aG docker "$USER"
fi
sudo docker info --format 'Docker {{.ServerVersion}} is running' || die "docker daemon did not come up; check 'sudo systemctl status docker'"

# ---------------------------------------------------------------- lingering
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = "yes" ]; then
    skip "lingering enabled for $USER"
else
    step "Enabling systemd lingering for $USER (creates /run/user/$(id -u), which 'just' needs)"
    sudo loginctl enable-linger "$USER"
fi

# ---------------------------------------------------------------- ~/.local/bin tools
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$HOME/.local/bin"

if command -v uv >/dev/null 2>&1; then
    skip "uv is installed ($(uv --version))"
else
    step "Installing uv (Python toolchain manager) into ~/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

if command -v just >/dev/null 2>&1; then
    skip "just is installed ($(just --version))"
else
    step "Installing just (task runner) into ~/.local/bin"
    curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to "$HOME/.local/bin"
fi

# ---------------------------------------------------------------- mngr checkout
if [ -z "$VERSION" ]; then
    if [ "$IS_DEV" = "1" ]; then
        VERSION="main"
    else
        step "Resolving the latest minds release tag"
        VERSION=$(git ls-remote --tags "$MNGR_REPO_URL" 'minds-v*' | awk -F/ '{print $NF}' | grep -v '\^{}' | sort -V | tail -1)
        [ -n "$VERSION" ] || die "could not resolve the latest minds-v* tag from $MNGR_REPO_URL"
        echo "    latest release: $VERSION"
    fi
fi

if [ -d "$INSTALL_DIR/.git" ]; then
    step "Updating existing mngr checkout at $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch -q --tags origin
    if [ -n "$(git -C "$INSTALL_DIR" status --porcelain)" ]; then
        die "the checkout at $INSTALL_DIR has uncommitted changes; commit/stash them or pass --install-dir for a separate checkout"
    fi
    git -C "$INSTALL_DIR" checkout -q "$VERSION"
    if git -C "$INSTALL_DIR" symbolic-ref -q HEAD >/dev/null; then
        git -C "$INSTALL_DIR" pull -q --ff-only origin "$VERSION"
    fi
else
    step "Cloning mngr ($VERSION) to $INSTALL_DIR"
    git clone -q "$MNGR_REPO_URL" "$INSTALL_DIR"
    git -C "$INSTALL_DIR" checkout -q "$VERSION"
fi

if [ "$IS_DEV" = "1" ]; then
    dwt_dir="$INSTALL_DIR/.external_worktrees/default-workspace-template"
    if [ -d "$dwt_dir/.git" ]; then
        skip "default-workspace-template checkout exists at $dwt_dir"
    else
        step "Cloning default-workspace-template (contributor layout) to $dwt_dir"
        mkdir -p "$INSTALL_DIR/.external_worktrees"
        git clone -q "$DWT_REPO_URL" "$dwt_dir"
    fi
fi

# ---------------------------------------------------------------- python env
step "Installing the Python workspace (uv sync --all-packages)"
(cd "$INSTALL_DIR" && uv sync -q --all-packages)

# ---------------------------------------------------------------- node toolchain
node_pin=$(tr -d '[:space:]' < "$INSTALL_DIR/apps/minds/.nvmrc")
pnpm_pin=$(jq -r '.engines.pnpm' "$INSTALL_DIR/apps/minds/package.json")
latchkey_pin=$(jq -r '.dependencies.latchkey' "$INSTALL_DIR/apps/minds/package.json" | sed 's/^[^0-9]*//')

export NVM_DIR="$HOME/.nvm"
if [ ! -s "$NVM_DIR/nvm.sh" ]; then
    step "Installing nvm (node version manager)"
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
fi
# nvm.sh trips set -u; relax around sourcing and version selection.
set +u
. "$NVM_DIR/nvm.sh"
if nvm ls "$node_pin" >/dev/null 2>&1; then
    skip "node $node_pin is installed"
else
    step "Installing node $node_pin (pinned by apps/minds/.nvmrc)"
    nvm install "$node_pin"
fi
nvm use "$node_pin" >/dev/null
set -u

if [ "$(pnpm --version 2>/dev/null || true)" = "$pnpm_pin" ]; then
    skip "pnpm $pnpm_pin is installed"
else
    step "Installing pnpm $pnpm_pin (pinned by apps/minds/package.json)"
    npm install -g "pnpm@$pnpm_pin" >/dev/null
fi

if [ "$(latchkey --version 2>/dev/null || true)" = "$latchkey_pin" ]; then
    skip "latchkey $latchkey_pin is installed"
else
    step "Installing latchkey $latchkey_pin (permissions gateway CLI, required by 'minds run')"
    npm install -g "latchkey@$latchkey_pin" >/dev/null
fi

step "Installing the Electron app dependencies (pnpm install)"
(cd "$INSTALL_DIR/apps/minds" && pnpm install --reporter=silent)

# ---------------------------------------------------------------- launcher
step "Writing the launcher to ~/.local/bin/minds-wsl-start"

if [ "$IS_DEV" = "1" ]; then
    launch_command='exec just minds-start'
else
    launch_command='cd apps/minds && exec pnpm start'
fi
cat > "$HOME/.local/bin/minds-wsl-start" <<LAUNCHER
#!/bin/bash
# Generated by install-wsl.sh -- launches the minds desktop app.
# Re-run install-wsl.sh to update the installation or change flags.
set -euo pipefail
export PATH="\$HOME/.local/bin:\$PATH"
export NVM_DIR="\$HOME/.nvm"
set +u
. "\$NVM_DIR/nvm.sh"
nvm use "$node_pin" >/dev/null
set -u
# Under WSL, default new workspaces to Docker's standard runc runtime: the
# WSL2 utility VM is the container/Windows isolation boundary (the same
# posture as the Docker VM on macOS). Opt into gVisor per-create instead by
# selecting runsc under the create form's advanced settings (requires
# 'runsc install -- --overlay2=none'; see apps/minds/docs/wsl.md).
export MINDS_DOCKER_RUNTIME_DEFAULT="\${MINDS_DOCKER_RUNTIME_DEFAULT:-runc}"
cd "$INSTALL_DIR"
eval "\$(uv run minds env activate "$ENV_NAME")"
$launch_command
LAUNCHER
chmod +x "$HOME/.local/bin/minds-wsl-start"

# ---------------------------------------------------------------- Windows shortcut
if command -v powershell.exe >/dev/null 2>&1; then
    step "Creating the 'Minds (WSL)' shortcut on the Windows desktop"
    distro_name="${WSL_DISTRO_NAME:-}"
    if [ -n "$distro_name" ]; then
        powershell.exe -NoProfile -NonInteractive -Command "
            \$desktop = [Environment]::GetFolderPath('Desktop')
            \$ws = New-Object -ComObject WScript.Shell
            \$sc = \$ws.CreateShortcut(\"\$desktop\\Minds (WSL).lnk\")
            \$sc.TargetPath = 'C:\\Windows\\System32\\wsl.exe'
            \$sc.Arguments = '-d $distro_name -- bash -lc ~/.local/bin/minds-wsl-start'
            \$sc.Description = 'Start the minds desktop app inside WSL'
            \$sc.Save()
        " >/dev/null || echo "    (shortcut creation failed; launch with: wsl -d $distro_name -- bash -lc ~/.local/bin/minds-wsl-start)"
    else
        echo "    (WSL_DISTRO_NAME is unset; skipping shortcut)"
    fi
else
    echo "    (powershell.exe not reachable from WSL; skipping shortcut)"
fi

# ---------------------------------------------------------------- launch
printf '\n\033[1;32mminds is installed.\033[0m (env: %s, version: %s, dir: %s)\n' "$ENV_NAME" "$VERSION" "$INSTALL_DIR"
echo "Start it any time with the 'Minds (WSL)' desktop shortcut, or: minds-wsl-start"

if [ "$IS_LAUNCH" = "1" ]; then
    step "Starting the minds desktop app"
    if id -nG | grep -qw docker; then
        exec "$HOME/.local/bin/minds-wsl-start"
    else
        # The docker group was added during this run; pick it up without re-login.
        exec sg docker -c "$HOME/.local/bin/minds-wsl-start"
    fi
fi
