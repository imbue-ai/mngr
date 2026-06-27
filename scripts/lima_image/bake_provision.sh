#!/usr/bin/env bash
# Bake the forever-claude-template toolchain into a Lima VM (issue 2306).
# Runs as root inside the VM (invoked by build-lima-image.sh via `limactl shell`).
#
# Runs the exact FCT build scripts the Lima provider runs at create time, so at
# create time the provisioning `command -v ... || install` guards short-circuit
# and the per-create workspace build hits warm caches. Finishes with cheap
# reproducibility cleanups so consecutive releases produce small desync deltas.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

: "${FCT_REPO_URL:?FCT_REPO_URL is required}"
: "${FCT_REF:?FCT_REF is required}"
# Pin mtimes for the cleanup pass (reproducibility helps upgrade deltas, not the
# first full download). Fixed instant; not "now", which would differ every build.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1700000000}"
# setup_system.sh installs uv into /root/.local/bin. install_dependencies.sh and
# build_workspace.sh add it to PATH themselves, but deferred_install.sh (Playwright)
# does not -- without this it fails with "uv: command not found" and Chromium never
# gets baked. Put it on PATH for the whole bake so every FCT script finds uv.
export PATH="/root/.local/bin:$PATH"

REPO_ROOT=/mngr/code

echo "==> Installing minimal bootstrap packages (git for clone, btrfs-progs for the data-disk mode)"
apt-get update
apt-get install -y --no-install-recommends ca-certificates git btrfs-progs
# /code is where agent work dirs land; the provider pre-creates it at create time
# (chmod 777). Bake it so the first boot has it already.
mkdir -p /code && chmod 777 /code

echo "==> Cloning forever-claude-template ${FCT_REF} into ${REPO_ROOT}"
mkdir -p "$(dirname "$REPO_ROOT")"
rm -rf "$REPO_ROOT"
git clone --depth 1 --branch "$FCT_REF" "$FCT_REPO_URL" "$REPO_ROOT"
git config --global --add safe.directory "$REPO_ROOT"

echo "==> Running the FCT toolchain build (setup_system -> install_dependencies -> build_workspace)"
# These are the exact scripts the Lima create template runs (FCT
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

echo "==> Bake complete for ${FCT_REF}"
