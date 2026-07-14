#!/usr/bin/env bash
# Bake the default-workspace-template toolchain into a Lima VM.
# Runs as root inside the VM (invoked by build-lima-image.sh via `limactl shell`).
#
# Runs the exact DEFAULT_WORKSPACE_TEMPLATE build scripts the Lima provider runs at create time, so at
# create time the provisioning `command -v ... || install` guards short-circuit
# and the per-create workspace build hits warm caches. Finishes with cheap
# reproducibility cleanups so consecutive releases produce small desync deltas.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# This script deletes every user account and wipes /home, which is correct inside the
# disposable bake VM and catastrophic on a real machine. Only build-lima-image.sh sets
# this marker, so a stray local run refuses instead of eating the operator's /home.
if [ "${MNGR_LIMA_BAKE:-}" != "1" ]; then
  echo "ERROR: this script wipes /home and must only run inside the bake VM." >&2
  echo "       Run scripts/build-lima-image.sh instead; it sets MNGR_LIMA_BAKE=1." >&2
  exit 1
fi

: "${DEFAULT_WORKSPACE_TEMPLATE_REPO_URL:?DEFAULT_WORKSPACE_TEMPLATE_REPO_URL is required}"
: "${DEFAULT_WORKSPACE_TEMPLATE_REF:?DEFAULT_WORKSPACE_TEMPLATE_REF is required}"
# Pin mtimes for the cleanup pass (reproducibility helps upgrade deltas, not the
# first full download). Fixed instant; not "now", which would differ every build.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1700000000}"
# setup_system.sh installs uv into /root/.local/bin. install_dependencies.sh and
# build_workspace.sh add it to PATH themselves, but deferred_install.sh (Playwright)
# does not -- without this it fails with "uv: command not found" and Chromium never
# gets baked. Put it on PATH for the whole bake so every DEFAULT_WORKSPACE_TEMPLATE script finds uv.
export PATH="/root/.local/bin:$PATH"

REPO_ROOT=/mngr/code

echo "==> Installing minimal bootstrap packages (git for clone, btrfs-progs for the data-disk mode)"
apt-get update
apt-get install -y --no-install-recommends ca-certificates git btrfs-progs
# /code is where agent work dirs land; the provider pre-creates it at create time
# (chmod 777). Bake it so the first boot has it already.
mkdir -p /code && chmod 777 /code

echo "==> Cloning default-workspace-template ${DEFAULT_WORKSPACE_TEMPLATE_REF} into ${REPO_ROOT}"
mkdir -p "$(dirname "$REPO_ROOT")"
rm -rf "$REPO_ROOT"
git clone --depth 1 --branch "$DEFAULT_WORKSPACE_TEMPLATE_REF" "$DEFAULT_WORKSPACE_TEMPLATE_REPO_URL" "$REPO_ROOT"
git config --global --add safe.directory "$REPO_ROOT"

echo "==> Running the DEFAULT_WORKSPACE_TEMPLATE toolchain build (setup_system -> install_dependencies -> build_workspace)"
# These are the exact scripts the Lima create template runs (DEFAULT_WORKSPACE_TEMPLATE
# .mngr/settings.toml [create_templates.lima] extra_provision_command), so baking
# them makes the create-time run idempotent + fast.
bash "$REPO_ROOT/scripts/setup_system.sh"
bash "$REPO_ROOT/scripts/install_dependencies.sh"
bash "$REPO_ROOT/scripts/build_workspace.sh"

echo "==> Baking deferred packages (Playwright/Chromium) so first boot does not pay for them"
# deferred_install.sh writes per-package markers under /var/lib/minds so the
# runtime one-shot no-ops once baked.
if [ -f "$REPO_ROOT/scripts/deferred_install.sh" ]; then
  bash "$REPO_ROOT/scripts/deferred_install.sh" || echo "WARNING: deferred_install.sh failed; first boot will install on demand"
fi

echo "==> Reproducibility + size cleanups (delta-friendly; CDC tolerates block shift)"
apt-get clean
rm -rf /var/lib/apt/lists/*
rm -rf /var/cache/* /tmp/* /var/tmp/* || true
find / -xdev -name '*.pyc' -delete 2>/dev/null || true
find / -xdev -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
# Truncate logs rather than delete (keep the files cloud-init/systemd expect).
find /var/log -type f -exec truncate -s 0 {} + 2>/dev/null || true
# Drop the bake instance's SSH host keys; the Lima provider injects/regenerates
# its own at create time.
rm -f /etc/ssh/ssh_host_* 2>/dev/null || true
: > /etc/machine-id 2>/dev/null || true
rm -f /var/lib/dbus/machine-id 2>/dev/null || true

echo "==> Removing the bake host's user so the image carries no host identity"
# Lima names the guest user after the host account and gives it that account's
# uid. macOS assigns its first account uid 501, so keeping the baker's user makes
# cloud-init's `useradd <their-name> --uid 501` fail with "UID 501 is not unique"
# on every host whose account is named differently -- which is every host but the
# baker's. The guest then has no user to accept Lima's key and the boot hangs on
# "Waiting for the essential requirement 1 of 3: ssh". cloud-init recreates the
# user from scratch on first boot, so the image must ship without one.
BAKE_USER="${SUDO_USER:-}"
if [ -z "$BAKE_USER" ] || [ "$BAKE_USER" = "root" ]; then
  echo "ERROR: cannot identify the bake user (SUDO_USER is '${BAKE_USER}'); refusing to bake a host-specific image" >&2
  exit 1
fi
# --force removes the account even though this very ssh session is running as it.
userdel --force --remove "$BAKE_USER" 2>/dev/null || true
groupdel "$BAKE_USER" 2>/dev/null || true
rm -rf /home/* 2>/dev/null || true
# cloud-init rewrites this for the real user on first boot.
rm -f /etc/sudoers.d/*cloud-init* 2>/dev/null || true

# The whole point of the image is that any host can boot it; a leftover human
# user silently reintroduces the uid collision, so fail the bake rather than
# publish an image that only boots on one Mac.
if awk -F: '$6 ~ /^\/home\//' /etc/passwd | grep .; then
  echo "ERROR: the image still carries the users above; they will collide with the booting host's uid" >&2
  exit 1
fi

echo "==> Resetting cloud-init so the image boots clean as a fresh instance"
cloud-init clean --logs --seed 2>/dev/null || cloud-init clean --logs 2>/dev/null || true

echo "==> Zeroing free space so empty blocks dedup/compress (fstrim, fallback to zero-fill)"
if command -v fstrim >/dev/null 2>&1 && fstrim -v / 2>/dev/null; then
  :
else
  dd if=/dev/zero of=/ZEROFILL bs=1M 2>/dev/null || true
  rm -f /ZEROFILL
fi
sync

echo "==> Bake complete for ${DEFAULT_WORKSPACE_TEMPLATE_REF}"
