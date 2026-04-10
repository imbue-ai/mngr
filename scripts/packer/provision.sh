#!/bin/sh
# Provision script for the mngr Lima base image.
# Installs all packages required by mngr hosts and the forever-claude-template.
# Supports both Alpine (apk) and Debian/Ubuntu (apt-get).
#
# Runs as root (packer connects as root). Uses #!/bin/sh because bash
# is not pre-installed on Alpine. Installs bash then re-execs for strict mode.
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v apk >/dev/null 2>&1; then
        sudo apk add --no-cache bash 2>/dev/null || apk add --no-cache bash
    fi
    exec bash "$0" "$@"
fi
set -euo pipefail

_install_claude_system_wide() {
    # Install Claude Code binary to /usr/local/bin so it's available to all users.
    # The official install script puts it in ~/.local/bin which is per-user.
    local gcs_bucket="https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases"
    local arch
    arch=$(uname -m)
    case "$arch" in
        x86_64)  arch="x64" ;;
        aarch64) arch="arm64" ;;
    esac
    local platform="linux-${arch}"
    local version
    version=$(curl -fsSL "$gcs_bucket/latest")
    local binary_url="$gcs_bucket/$version/$platform/claude"
    echo "Installing Claude Code $version to /usr/local/bin..."
    curl -fsSL "$binary_url" -o /usr/local/bin/claude
    chmod +x /usr/local/bin/claude
}

if command -v apk >/dev/null 2>&1; then
    # Alpine
    apk add --no-cache \
        bash tmux git git-lfs jq rsync curl xxd openssh-server \
        ca-certificates build-base python3 py3-pip \
        ripgrep fd less nano sqlite procps unison wget \
        nodejs npm shadow sudo gcompat

    # Install uv to /usr/local/bin (system-wide, not per-user)
    curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh

    # Install Claude Code to /usr/local/bin (system-wide).
    # The install script puts it in ~/.local/bin, so we download the binary
    # directly and place it in /usr/local/bin for all users.
    _install_claude_system_wide

    # Install ttyd
    ARCH=$(uname -m)
    curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.${ARCH}" \
        -o /usr/local/bin/ttyd && chmod +x /usr/local/bin/ttyd

    # Install cloudflared
    ARCH_DEB=$([ "$(uname -m)" = "aarch64" ] && echo "arm64" || echo "amd64")
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH_DEB}" \
        -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared

    # Install GitHub CLI
    apk add --no-cache github-cli 2>/dev/null || true

    # Configure sshd for mngr (high session/startup limits)
    if ! grep -q '^MaxSessions' /etc/ssh/sshd_config 2>/dev/null; then
        cat >> /etc/ssh/sshd_config <<SSHD_EOF
MaxSessions 100
MaxStartups 100:30:200
SSHD_EOF
    fi

    # Ensure sshd starts on boot
    rc-update add sshd default 2>/dev/null || true

    # Create /code directory
    mkdir -p /code && chmod 777 /code

elif command -v apt-get >/dev/null 2>&1; then
    # Debian/Ubuntu
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        bash build-essential ca-certificates curl fd-find git git-lfs jq \
        less nano openssh-server procps ripgrep rsync sqlite3 \
        tmux unison wget xxd

    # Install uv to /usr/local/bin
    curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh

    # Install Claude Code to /usr/local/bin
    _install_claude_system_wide

    # Install ttyd
    ARCH=$(uname -m)
    curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.${ARCH}" \
        -o /usr/local/bin/ttyd && chmod +x /usr/local/bin/ttyd

    # Install cloudflared
    ARCH_DEB=$(dpkg --print-architecture)
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH_DEB}" \
        -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared

    # Install GitHub CLI
    mkdir -p -m 755 /etc/apt/keyrings
    wget -nv -O /tmp/githubcli.gpg https://cli.github.com/packages/githubcli-archive-keyring.gpg
    cat /tmp/githubcli.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli.list > /dev/null
    apt-get update -qq && apt-get install -y -qq gh

    # Install Node.js
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs

    # Configure sshd
    if ! grep -q '^MaxSessions' /etc/ssh/sshd_config 2>/dev/null; then
        cat >> /etc/ssh/sshd_config <<SSHD_EOF
MaxSessions 100
MaxStartups 100:30:200
SSHD_EOF
    fi

    mkdir -p /run/sshd
    mkdir -p /code && chmod 777 /code

    apt-get clean
    rm -rf /var/lib/apt/lists/*
fi
