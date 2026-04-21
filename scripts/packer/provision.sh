#!/bin/bash
# Provision script for the mngr Lima base image.
# Bakes in every tool the Lima VM needs so that first-boot has no network installs.
# Keep in sync with libs/mngr/imbue/mngr/resources/Dockerfile.
set -euo pipefail

# Claude Code version to install. Passed in from Packer via `environment_vars`.
# Empty means "latest" (matches Dockerfile behavior).
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-}"

sudo apt-get update -qq

# Base system utilities + everything the Dockerfile's apt layer installs.
sudo apt-get install -y -qq --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    fd-find \
    git \
    git-lfs \
    jq \
    openssh-server \
    ripgrep \
    rsync \
    sqlite3 \
    tmux \
    unison \
    wget \
    xxd

# sshd run directory (cloud-init normally creates this; we do it here so the
# image is usable even if cloud-init is disabled).
sudo mkdir -p /run/sshd

# Node.js 20.x from NodeSource. Required by Claude Code at runtime and by the
# workspace server frontend build.
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y -qq nodejs

# gh CLI from GitHub's apt repo.
sudo mkdir -p -m 755 /etc/apt/keyrings
wget -nv -O- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt-get update -qq
sudo apt-get install -y -qq gh

# ttyd (web terminal) from GitHub releases.
ARCH=$(uname -m)
sudo curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.${ARCH}" \
    -o /usr/local/bin/ttyd
sudo chmod +x /usr/local/bin/ttyd

# uv (Python package manager) installed system-wide so every VM user sees it.
# UV_INSTALL_DIR places the binary directly in /usr/local/bin; UV_UNMANAGED_INSTALL
# prevents the installer from editing per-user shell rc files.
sudo env UV_INSTALL_DIR=/usr/local/bin UV_UNMANAGED_INSTALL=1 \
    bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'

# Claude Code. The installer always writes to $HOME/.local/bin, so we run it as
# root and then copy the binary system-wide. Install is idempotent-ish: if a
# pinned version is requested, `install.sh` replaces whatever is there.
sudo bash -c "
    set -euo pipefail
    curl -fsSL https://claude.ai/install.sh -o /tmp/install_claude.sh
    if [ -n '${CLAUDE_CODE_VERSION}' ]; then
        HOME=/root bash /tmp/install_claude.sh '${CLAUDE_CODE_VERSION}'
    else
        HOME=/root bash /tmp/install_claude.sh
    fi
    test -x /root/.local/bin/claude
    install -m 0755 /root/.local/bin/claude /usr/local/bin/claude
    rm -f /tmp/install_claude.sh
"

# Shrink the image: remove apt caches and any downloaded package lists.
sudo apt-get clean
sudo rm -rf /var/lib/apt/lists/*
