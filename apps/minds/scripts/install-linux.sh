#!/usr/bin/env bash
# Idempotent bring-up of the minds desktop app on Linux, run from source
# against production. Covers a plain Debian/Ubuntu machine, an Ubuntu/Debian
# WSL2 distro (auto-detected; see docs/wsl.md), and a Raspberry Pi 5 turned
# into an always-on host (--raspberry-pi; see docs/raspberry-pi.md).
#
# Two ways to run it:
#
#   From an existing checkout -- uses that checkout, clones nothing:
#     apps/minds/scripts/install-linux.sh
#
#   Curl-piped bootstrap -- clones the public repo to --install-dir:
#     curl -fsSL https://raw.githubusercontent.com/imbue-ai/mngr/main/apps/minds/scripts/install-linux.sh | bash
#
# Everything the script touches is on the public mirror; it needs no Imbue
# account, credentials, or private tooling.
#
# Flags (curl-piped: `bash -s -- <flags>`):
#   --version REF      `latest` resolves the newest public minds-v* tag; any
#                      other value is a git ref to check out. Without it an
#                      existing checkout is left exactly as it is, and a fresh
#                      clone lands on main.
#   --install-dir DIR  Where to clone when there is no checkout (default ~/mngr;
#                      a relative path is resolved against the current directory).
#   --non-interactive  Never prompt. Privileged steps that are not already done
#                      print their exact command and exit 1; the $HOME toolchain
#                      installs and a fresh clone proceed.
#   --yes              Answer yes to every prompt (privileged steps run).
#   --skip-docker      Skip the Docker CE install and daemon check (not with
#                      --raspberry-pi, whose setup registers runsc with Docker).
#   --no-launch        Set everything up but do not start the app.
#   --raspberry-pi     Raspberry Pi 5 always-on host: Docker CE, gVisor runsc, the
#                      kernel + boot changes it needs, desktop autologin, wayvnc,
#                      and menu/autostart entries.
#
# Install policy: the $HOME toolchain (uv, nvm, the pinned Node, the pinned
# pnpm) installs after one confirmation, and a fresh clone asks before it
# lands in --install-dir (neither asks under --non-interactive); the
# checkout's venv and node_modules are synced on every run. Anything needing
# sudo (apt packages, Docker CE, docker group membership, systemd lingering,
# the Pi boot/kernel/desktop changes) prompts with the exact command first.
set -euo pipefail

MNGR_REPO_URL="https://github.com/imbue-ai/mngr.git"
NVM_VERSION="v0.40.3"
# Same gVisor release the mngr_vps host setup pins (PINNED_GVISOR_RELEASE);
# apps/minds/scripts/build_test.py checks the two agree.
GVISOR_RELEASE="20260601"
# Debian's docker.io is 20.10; mngr's isolated host volumes need Engine >= 25.
MIN_DOCKER_MAJOR=25
# Workspace images are what fill a disk; the checkout, its venv, node_modules
# and the bundled binaries fit in a few GB, so --skip-docker needs far less.
MIN_FREE_DISK_GB=20
MIN_FREE_DISK_GB_WITHOUT_DOCKER=5
LAUNCHER="$HOME/.local/bin/minds-desktop"
# The launcher path as one shell word, for the places that hand it to a shell
# as part of a command string rather than as an argument.
LAUNCHER_WORD="$(printf '%q' "$LAUNCHER")"
# Not $USER: login shells set it, but `docker run`, some `ssh host cmd` setups
# and systemd units do not, and `set -u` would abort on the first expansion.
CURRENT_USER="$(id -un)"

VERSION=""
INSTALL_DIR="$HOME/mngr"
IS_NON_INTERACTIVE=0
IS_YES=0
IS_SKIP_DOCKER=0
IS_LAUNCH=1
IS_RASPBERRY_PI=0

while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --non-interactive) IS_NON_INTERACTIVE=1; shift ;;
        --yes) IS_YES=1; shift ;;
        --skip-docker) IS_SKIP_DOCKER=1; shift ;;
        --no-launch) IS_LAUNCH=0; shift ;;
        --raspberry-pi) IS_RASPBERRY_PI=1; shift ;;
        *) echo "error: unknown flag: $1" >&2; exit 2 ;;
    esac
done
if [ "$IS_NON_INTERACTIVE" = 1 ] && [ "$IS_YES" = 1 ]; then
    echo "error: --non-interactive and --yes are mutually exclusive (--yes already never prompts)" >&2
    exit 2
fi
if [ "$IS_RASPBERRY_PI" = 1 ] && [ "$IS_SKIP_DOCKER" = 1 ]; then
    echo "error: --raspberry-pi and --skip-docker are mutually exclusive (the Pi setup registers runsc with Docker)" >&2
    exit 2
fi
# The launcher and the desktop entry embed this path and run from other
# directories, so a relative --install-dir has to be pinned down here.
INSTALL_DIR="$(realpath -m "$INSTALL_DIR")"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
skip() { printf '\033[2m    (already done: %s)\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# Ask a yes/no question on the terminal. Under --yes the answer is yes; under
# --non-interactive the answer is no. Reads from /dev/tty so it works when the
# script itself arrives on stdin (curl | bash).
confirm() {
    local question="$1"
    if [ "$IS_YES" = 1 ]; then return 0; fi
    if [ "$IS_NON_INTERACTIVE" = 1 ]; then return 1; fi
    # `test -r /dev/tty` only checks the device node's permission bits, which
    # pass even with no controlling terminal; opening it is the real check.
    if ! { : < /dev/tty; } 2>/dev/null; then
        die "no terminal to prompt on; re-run with --yes (run every step) or --non-interactive"
    fi
    local answer
    printf '%s [Y/n] ' "$question" > /dev/tty
    read -r answer < /dev/tty
    case "$answer" in
        ""|y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# Run a privileged command after showing it. Under --non-interactive an
# undone step is an error that names the command, so the user can run it by
# hand and re-run the script.
run_privileged() {
    local description="$1"
    shift
    printf '    %s needs sudo:\n      sudo' "$description"
    printf ' %q' "$@"
    printf '\n'
    if [ "$IS_NON_INTERACTIVE" = 1 ]; then
        die "$description is not done and --non-interactive forbids prompting. Run the command above, then re-run."
    fi
    if ! confirm "    Run it now?"; then
        die "$description was declined. Run the command above, then re-run."
    fi
    sudo "$@"
}

# Same as run_privileged, for a command that needs a root shell (pipes,
# redirections). The command is passed as one string.
run_privileged_shell() {
    local description="$1"
    local command_text="$2"
    printf '    %s needs sudo:\n      sudo sh -c %q\n' "$description" "$command_text"
    if [ "$IS_NON_INTERACTIVE" = 1 ]; then
        die "$description is not done and --non-interactive forbids prompting. Run the command above, then re-run."
    fi
    if ! confirm "    Run it now?"; then
        die "$description was declined. Run the command above, then re-run."
    fi
    sudo sh -c "$command_text"
}

# ---------------------------------------------------------------- preflight
step "Preflight checks"

if [ "$(id -u)" = "0" ]; then
    die "run this as your normal user, not root -- the script uses sudo where needed"
fi
if [ "$(uname -s)" != "Linux" ]; then
    die "this script sets up minds on Linux; see apps/minds/docs/dev-setup.md for macOS"
fi
if ! command -v apt-get >/dev/null 2>&1; then
    die "this script supports Debian/Ubuntu (apt-get not found)"
fi

IS_WSL=0
if grep -qi microsoft /proc/version 2>/dev/null; then
    IS_WSL=1
    if ! uname -r | grep -qi 'WSL2\|microsoft-standard'; then
        die "this looks like WSL1. minds needs WSL2 (Docker and systemd require the real kernel).
Fix from Windows:  wsl --set-version ${WSL_DISTRO_NAME:-<distro>} 2"
    fi
    if [ "$(ps -p 1 -o comm=)" != "systemd" ]; then
        if [ ! -e /etc/wsl.conf ]; then
            run_privileged_shell "Enabling systemd in /etc/wsl.conf (required for Docker)" \
                "printf '[boot]\nsystemd=true\n' > /etc/wsl.conf"
            die "systemd enabled, but the distro must restart for it to take effect.
From Windows run:  wsl --shutdown
Then re-run this script."
        fi
        die "PID 1 is not systemd and /etc/wsl.conf already exists.
Add the following to /etc/wsl.conf, then run 'wsl --shutdown' from Windows and re-run:
[boot]
systemd=true"
    fi
    case "$INSTALL_DIR" in
        /mnt/*) die "--install-dir must be inside the Linux filesystem (e.g. ~/mngr), not under /mnt/ -- the Windows filesystem breaks git line endings and is drastically slower" ;;
        *) : ;;
    esac
    if [ "$IS_SKIP_DOCKER" != 1 ] && command -v docker >/dev/null 2>&1; then
        case "$(readlink -f "$(command -v docker)")" in
            *docker-desktop*) die "this distro's docker comes from Docker Desktop's WSL integration, which minds has not been verified against (and which cannot register alternative runtimes).
Either disable Docker Desktop's integration for this distro (Docker Desktop -> Settings -> Resources -> WSL integration) so this script can install Docker CE, or use a separate distro." ;;
            *) : ;;
        esac
    fi
fi

if [ "$IS_RASPBERRY_PI" = 1 ]; then
    if [ ! -f /boot/firmware/kernel8.img ]; then
        die "/boot/firmware/kernel8.img is missing; gVisor needs the 4K-page kernel (is this 64-bit Raspberry Pi OS?)"
    fi
    if ! command -v raspi-config >/dev/null 2>&1; then
        die "raspi-config not found; --raspberry-pi expects Raspberry Pi OS"
    fi
fi

# ---------------------------------------------------------------- apt packages
step "System packages (base tools + Electron runtime libraries)"

# Installs the first available candidate for each |-separated group, so Ubuntu
# 24.04's t64-suffixed library names and Debian's plain ones both resolve.
apt_groups=(
    git tmux jq curl rsync ca-certificates build-essential openssh-client
    "libgtk-3-0t64|libgtk-3-0"
    libnss3
    "libasound2t64|libasound2"
    "libatk-bridge2.0-0t64|libatk-bridge2.0-0"
    libgbm1
)
if [ "$IS_RASPBERRY_PI" = 1 ]; then
    # grim takes Wayland screenshots for the Pi's remote-desktop workflow.
    apt_groups+=(grim)
fi
missing_packages=()
for group in "${apt_groups[@]}"; do
    IFS='|' read -ra candidates <<< "$group"
    is_group_installed=0
    for candidate in "${candidates[@]}"; do
        if [ "$(dpkg-query -W -f='${Status}' "$candidate" 2>/dev/null || true)" = "install ok installed" ]; then
            is_group_installed=1
            break
        fi
    done
    if [ "$is_group_installed" = 0 ]; then
        missing_packages+=("$group")
    fi
done
if [ "${#missing_packages[@]}" = 0 ]; then
    skip "all packages installed"
else
    # Resolve each missing group to whichever candidate this distro carries.
    # Needs the package lists, so it runs as part of the privileged command.
    # shellcheck disable=SC2016  # a script for the root shell, expanded there
    resolve_script='
        set -e
        apt-get update -qq
        chosen=""
        for group in "$@"; do
            found=""
            for candidate in $(printf "%s" "$group" | tr "|" " "); do
                if apt-cache show "$candidate" >/dev/null 2>&1; then found="$candidate"; break; fi
            done
            if [ -z "$found" ]; then echo "error: none of the package candidates $group exist in this apt archive" >&2; exit 1; fi
            chosen="$chosen $found"
        done
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $chosen
    '
    run_privileged "Installing ${missing_packages[*]}" sh -c "$resolve_script" sh "${missing_packages[@]}"
fi

# ---------------------------------------------------------------- checkout
# Run from inside a checkout, use it (BASH_SOURCE is empty when piped in).
CHECKOUT=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$script_dir/../.nvmrc" ] && [ -f "$script_dir/../../../pyproject.toml" ]; then
        CHECKOUT="$(cd "$script_dir/../../.." && pwd)"
    fi
fi
if [ -z "$CHECKOUT" ] && [ -f "$INSTALL_DIR/apps/minds/.nvmrc" ]; then
    CHECKOUT="$INSTALL_DIR"
fi

# The clone, the venv and node_modules all land under the checkout; measure
# its filesystem. A fresh --install-dir may not exist yet (git creates the
# leading directories), so walk up to its nearest existing ancestor.
disk_check_dir="${CHECKOUT:-$INSTALL_DIR}"
while [ ! -d "$disk_check_dir" ]; do
    disk_check_dir="$(dirname "$disk_check_dir")"
done
available_kb=$(df -Pk "$disk_check_dir" | awk 'NR==2 {print $4}')
min_free_gb="$MIN_FREE_DISK_GB"
if [ "$IS_SKIP_DOCKER" = 1 ]; then
    min_free_gb="$MIN_FREE_DISK_GB_WITHOUT_DOCKER"
fi
if [ "$available_kb" -lt $((min_free_gb * 1024 * 1024)) ]; then
    die "less than ${min_free_gb}GB free on the filesystem holding $disk_check_dir ($((available_kb / 1024 / 1024))GB available). Free up disk space first."
fi

if [ "$VERSION" = "latest" ]; then
    step "Resolving the latest minds release tag"
    # grep -v exits 1 when there are no tags at all.
    VERSION=$(git ls-remote --tags "$MNGR_REPO_URL" 'minds-v*' | awk -F/ '{print $NF}' | grep -v '\^{}' | sort -V | tail -1 || true)
    [ -n "$VERSION" ] || die "could not resolve the latest minds-v* tag from $MNGR_REPO_URL"
    echo "    latest release: $VERSION"
fi

if [ -z "$CHECKOUT" ]; then
    step "Cloning mngr (${VERSION:-main}) to $INSTALL_DIR"
    if [ "$IS_NON_INTERACTIVE" != 1 ] && ! confirm "    Clone into $INSTALL_DIR?"; then
        die "clone declined; pass --install-dir to choose another location"
    fi
    git clone -q "$MNGR_REPO_URL" "$INSTALL_DIR"
    if [ -n "$VERSION" ]; then
        git -C "$INSTALL_DIR" checkout -q "$VERSION"
    fi
    CHECKOUT="$INSTALL_DIR"
elif [ -n "$VERSION" ]; then
    step "Checking out $VERSION in $CHECKOUT"
    if [ -n "$(git -C "$CHECKOUT" status --porcelain)" ]; then
        die "the checkout at $CHECKOUT has uncommitted changes; commit or stash them before --version"
    fi
    git -C "$CHECKOUT" fetch -q --tags origin
    git -C "$CHECKOUT" checkout -q "$VERSION"
    if git -C "$CHECKOUT" symbolic-ref -q HEAD >/dev/null; then
        git -C "$CHECKOUT" pull -q --ff-only origin "$VERSION"
    fi
else
    step "Using the checkout at $CHECKOUT"
fi

NODE_VERSION="$(tr -d '[:space:]' < "$CHECKOUT/apps/minds/.nvmrc")"

# ---------------------------------------------------------------- docker
if [ "$IS_SKIP_DOCKER" = 1 ]; then
    step "Skipping Docker (--skip-docker)"
else
    step "Docker Engine"
    if command -v docker >/dev/null 2>&1; then
        docker_major="$(docker --version 2>/dev/null | sed -n 's/^Docker version \([0-9]*\).*/\1/p' || true)"
        if [ -z "$docker_major" ] || [ "$docker_major" -lt "$MIN_DOCKER_MAJOR" ]; then
            die "docker $(docker --version 2>/dev/null || echo '(unknown version)') is older than ${MIN_DOCKER_MAJOR}.x; mngr's isolated host volumes need Engine >= ${MIN_DOCKER_MAJOR}.
Remove the distro package and install Docker CE:  curl -fsSL https://get.docker.com | sudo sh"
        fi
        skip "docker is installed ($(docker --version))"
    else
        run_privileged_shell "Installing Docker CE (via get.docker.com)" "curl -fsSL https://get.docker.com | sh"
    fi
    if command -v systemctl >/dev/null 2>&1 && ! systemctl is-enabled --quiet docker 2>/dev/null; then
        run_privileged "Enabling the docker service" systemctl enable --now docker
    fi
    if id -nG "$CURRENT_USER" | grep -qw docker; then
        skip "$CURRENT_USER is in the docker group"
    else
        run_privileged "Adding $CURRENT_USER to the docker group" usermod -aG docker "$CURRENT_USER"
        echo "    (group membership applies to new logins; this run uses 'sg docker' to launch)"
    fi
    if ! sg docker -c 'docker info >/dev/null 2>&1'; then
        die "the docker daemon is not reachable as $CURRENT_USER. Check 'sudo systemctl status docker', then log out and back in (or run 'newgrp docker') so the docker group applies, and re-run."
    fi
fi

# ---------------------------------------------------------------- raspberry pi
IS_REBOOT_NEEDED=0
if [ "$IS_RASPBERRY_PI" = 1 ]; then
    step "Raspberry Pi: gVisor runsc, registered the way the mngr_vps host setup registers it"
    if command -v runsc >/dev/null 2>&1; then
        skip "runsc is installed"
    else
        gvisor_script="
            set -e
            url=https://storage.googleapis.com/gvisor/releases/release/${GVISOR_RELEASE}/\$(uname -m)
            tmp=\$(mktemp -d)
            cd \"\$tmp\"
            curl -fsSL -o runsc \"\$url/runsc\"
            curl -fsSL -o runsc.sha512 \"\$url/runsc.sha512\"
            curl -fsSL -o containerd-shim-runsc-v1 \"\$url/containerd-shim-runsc-v1\"
            curl -fsSL -o containerd-shim-runsc-v1.sha512 \"\$url/containerd-shim-runsc-v1.sha512\"
            sha512sum -c runsc.sha512 containerd-shim-runsc-v1.sha512
            chmod a+rx runsc containerd-shim-runsc-v1
            mv runsc containerd-shim-runsc-v1 /usr/local/bin/
            cd / && rm -rf \"\$tmp\"
        "
        run_privileged_shell "Installing gVisor runsc ${GVISOR_RELEASE}" "$gvisor_script"
    fi
    if grep -q -- '--overlay2=none' /etc/docker/daemon.json 2>/dev/null; then
        skip "runsc is registered with Docker"
    else
        # Register whichever runsc the skip check found (gVisor's apt package
        # puts it in /usr/bin); the fallback is where the install above lands it.
        runsc_path="$(command -v runsc || echo /usr/local/bin/runsc)"
        run_privileged_shell "Registering runsc with Docker (--overlay2=none)" \
            "$runsc_path install -- --overlay2=none && systemctl restart docker"
    fi

    step "Raspberry Pi: kernel settings gVisor needs (memory cgroup, 4K pages)"
    cmdline=/boot/firmware/cmdline.txt
    if grep -q 'cgroup_enable=memory' "$cmdline"; then
        skip "memory cgroup enabled in $cmdline"
    else
        run_privileged "Enabling the memory cgroup controller in $cmdline" \
            sed -i '1 s/$/ cgroup_enable=cpuset cgroup_enable=memory cgroup_memory=1/' "$cmdline"
        IS_REBOOT_NEEDED=1
    fi
    config=/boot/firmware/config.txt
    if grep -q '^kernel=kernel8.img' "$config"; then
        skip "4K-page kernel selected in $config"
    else
        # [all] ends whatever model filter section precedes it, so the override
        # applies regardless of how config.txt currently ends.
        run_privileged_shell "Selecting the 4K-page kernel8.img in $config" \
            "printf '\n[all]\n# 4K-page kernel: gVisor (runsc) needs 4K pages; the default kernel_2712.img uses 16K pages.\nkernel=kernel8.img\n' >> $config"
        IS_REBOOT_NEEDED=1
    fi

    step "Raspberry Pi: boot to the desktop with autologin and serve it over wayvnc"
    # A stock desktop image already boots to lightdm, so the target and the
    # display manager alone do not tell whether this step has run: the
    # autologin user (what raspi-config B4 writes) and wayvnc are checked too.
    is_pi_desktop_configured() {
        [ "$(systemctl get-default)" = "graphical.target" ] \
            && systemctl is-enabled --quiet lightdm.service \
            && grep -q "^autologin-user=$CURRENT_USER\$" /etc/lightdm/lightdm.conf 2>/dev/null \
            && systemctl is-enabled --quiet wayvnc.service
    }
    if is_pi_desktop_configured; then
        skip "desktop autologin and wayvnc configured"
    else
        # raspi-config only switches the default target; a headless install may
        # have the display manager itself disabled. RealVNC's X11 service cannot
        # capture a Wayland session, so wayvnc replaces it.
        run_privileged_shell "Configuring desktop autologin + wayvnc" \
            "raspi-config nonint do_boot_behaviour B4 && systemctl enable lightdm.service && (systemctl disable --now vncserver-x11-serviced.service 2>/dev/null || true) && systemctl enable wayvnc.service"
        IS_REBOOT_NEEDED=1
    fi
fi

# ---------------------------------------------------------------- lingering (WSL)
if [ "$IS_WSL" = 1 ]; then
    # WSL tears the distro down shortly after the last session exits; a
    # lingering user session keeps the app's background daemons alive.
    if [ "$(loginctl show-user "$CURRENT_USER" -p Linger --value 2>/dev/null)" = "yes" ]; then
        skip "lingering enabled for $CURRENT_USER"
    else
        run_privileged "Enabling systemd lingering for $CURRENT_USER" loginctl enable-linger "$CURRENT_USER"
    fi
fi

# ---------------------------------------------------------------- $HOME toolchain
step "Toolchain under \$HOME: uv, nvm, Node ${NODE_VERSION}, pnpm"
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$HOME/.local/bin"
export NVM_DIR="$HOME/.nvm"

# Asked once, the first time a toolchain install is actually needed; the
# answer covers the rest of the run.
IS_TOOLCHAIN_CONFIRMED=0
confirm_toolchain_install() {
    if [ "$IS_TOOLCHAIN_CONFIRMED" = 1 ]; then return 0; fi
    if [ "$IS_NON_INTERACTIVE" != 1 ] && ! confirm "    Install what is missing under $HOME (uv, nvm, Node ${NODE_VERSION}, pnpm)?"; then
        die "toolchain install declined. Install uv (https://docs.astral.sh/uv/) and nvm (https://github.com/nvm-sh/nvm), then re-run."
    fi
    IS_TOOLCHAIN_CONFIRMED=1
}

if command -v uv >/dev/null 2>&1; then
    skip "uv is installed ($(uv --version))"
else
    confirm_toolchain_install
    echo "    Installing uv into ~/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

if [ -s "$NVM_DIR/nvm.sh" ]; then
    skip "nvm is installed"
else
    confirm_toolchain_install
    echo "    Installing nvm ${NVM_VERSION}"
    curl -fsSL "https://raw.githubusercontent.com/nvm-sh/nvm/${NVM_VERSION}/install.sh" | PROFILE=/dev/null bash
fi
# nvm.sh references unbound variables.
set +u
# shellcheck disable=SC1091
. "$NVM_DIR/nvm.sh"
if nvm ls "$NODE_VERSION" >/dev/null 2>&1; then
    skip "node ${NODE_VERSION} is installed"
else
    confirm_toolchain_install
    echo "    Installing node ${NODE_VERSION} (pinned by apps/minds/.nvmrc)"
    nvm install "$NODE_VERSION"
fi
nvm use "$NODE_VERSION" >/dev/null
set -u

PNPM_VERSION="$(node -p "require('$CHECKOUT/apps/minds/package.json').engines.pnpm")"
if [ -z "$PNPM_VERSION" ] || [ "$PNPM_VERSION" = "undefined" ]; then
    die "no engines.pnpm pin in $CHECKOUT/apps/minds/package.json"
fi
# Checked from apps/minds: pnpm self-switches to the nearest package.json's
# pin, so a check from elsewhere can report an unrelated version.
if [ "$(cd "$CHECKOUT/apps/minds" && pnpm --version 2>/dev/null || true)" = "$PNPM_VERSION" ]; then
    skip "pnpm ${PNPM_VERSION} is installed"
else
    confirm_toolchain_install
    echo "    Installing pnpm ${PNPM_VERSION} (pinned by apps/minds/package.json)"
    npm install -g "pnpm@${PNPM_VERSION}" >/dev/null
fi

# ---------------------------------------------------------------- build the checkout
step "Python workspace (uv sync --all-packages)"
(cd "$CHECKOUT" && uv sync -q --all-packages)

step "Electron app dependencies (pnpm install)"
# Playwright's browsers are only for the e2e suite.
(cd "$CHECKOUT/apps/minds" && PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 pnpm install --frozen-lockfile --reporter=silent)

step "Bundled binaries and the UI bundle (what 'pnpm start' does before every launch)"
(cd "$CHECKOUT/apps/minds" && node scripts/ensure-binaries.js && pnpm run build:ui)

# ---------------------------------------------------------------- launcher
step "Launcher: $LAUNCHER"
build_timeout_export=""
if [ "$IS_RASPBERRY_PI" = 1 ]; then
    # The first workspace image build on a Pi runs well past mngr's default
    # 600s docker build timeout.
    build_timeout_export='export MNGR__PROVIDERS__DOCKER__BUILD_TIMEOUT_SECONDS=3600
'
fi
cat > "$LAUNCHER" <<LAUNCHER_SCRIPT
#!/usr/bin/env bash
# Generated by apps/minds/scripts/install-linux.sh -- launches the minds
# desktop app from the source checkout, against production. Re-run the
# installer to update the checkout or change flags.
set -euo pipefail
export PATH="\$HOME/.local/bin:\$PATH"
${build_timeout_export}exec "${CHECKOUT}/apps/minds/scripts/start-desktop.sh"
LAUNCHER_SCRIPT
chmod +x "$LAUNCHER"

if [ "$IS_RASPBERRY_PI" = 1 ]; then
    step "Raspberry Pi: menu entry and autostart on login"
    mkdir -p "$HOME/.local/share/applications" "$HOME/.config/autostart"
    cat > "$HOME/.local/share/applications/minds.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Mind
Comment=Mind desktop client (from source)
Exec="$LAUNCHER"
Icon=$CHECKOUT/apps/minds/electron/assets/icon.png
Terminal=false
Categories=Development;
DESKTOP
    cp "$HOME/.local/share/applications/minds.desktop" "$HOME/.config/autostart/minds.desktop"
fi

if [ "$IS_WSL" = 1 ]; then
    if command -v powershell.exe >/dev/null 2>&1 && [ -n "${WSL_DISTRO_NAME:-}" ]; then
        step "Creating the 'Mind (WSL)' shortcut on the Windows desktop"
        powershell.exe -NoProfile -NonInteractive -Command "
            \$desktop = [Environment]::GetFolderPath('Desktop')
            \$ws = New-Object -ComObject WScript.Shell
            \$sc = \$ws.CreateShortcut(\"\$desktop\\Mind (WSL).lnk\")
            \$sc.TargetPath = 'C:\\Windows\\System32\\wsl.exe'
            \$sc.Arguments = '-d $WSL_DISTRO_NAME -- bash -lc \"$LAUNCHER_WORD\"'
            \$sc.Description = 'Start the minds desktop app inside WSL'
            \$sc.Save()
        " >/dev/null || echo "    (shortcut creation failed; launch with: wsl -d $WSL_DISTRO_NAME -- bash -lc \"$LAUNCHER_WORD\")"
    else
        echo "    (powershell.exe or WSL_DISTRO_NAME unavailable; skipping the Windows shortcut)"
    fi
fi

# ---------------------------------------------------------------- done
printf '\n\033[1;32mminds is installed.\033[0m (checkout: %s)\n' "$CHECKOUT"
echo "Start it any time with: $LAUNCHER"
if [ "$IS_REBOOT_NEEDED" = 1 ]; then
    echo "REBOOT REQUIRED: kernel command line / kernel image / boot target changed (sudo reboot)"
    exit 0
fi

if [ "$IS_LAUNCH" = 1 ]; then
    step "Starting the minds desktop app"
    if [ "$IS_SKIP_DOCKER" = 1 ] || id -nG | grep -qw docker; then
        exec "$LAUNCHER"
    else
        # The docker group was added during this run; pick it up without re-login.
        exec sg docker -c "$LAUNCHER_WORD"
    fi
fi
