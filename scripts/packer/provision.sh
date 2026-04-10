#!/bin/sh
# Provision script for the mngr Lima base image.
# Installs all packages required by mngr hosts and the forever-claude-template.
# Supports both Alpine (apk) and Debian/Ubuntu (apt-get).
#
# Runs as root (packer connects as root). Uses #!/bin/sh because bash
# is not pre-installed on Alpine. Installs bash then re-execs for strict mode.
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v apk >/dev/null 2>&1; then
        apk add --no-cache bash
    fi
    exec bash "$0" "$@"
fi
set -euo pipefail

if command -v apk >/dev/null 2>&1; then
    # Alpine
    apk add --no-cache \
        bash tmux git git-lfs jq rsync curl xxd openssh-server \
        ca-certificates build-base python3 py3-pip \
        ripgrep fd less nano sqlite procps unison wget \
        nodejs npm shadow sudo

    # Install uv
    curl -LsSf https://astral.sh/uv/install.sh | sh

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
    cat >> /etc/ssh/sshd_config <<SSHD_EOF
MaxSessions 100
MaxStartups 100:30:200
SSHD_EOF

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

    # Install uv
    curl -LsSf https://astral.sh/uv/install.sh | sh

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
    cat >> /etc/ssh/sshd_config <<SSHD_EOF
MaxSessions 100
MaxStartups 100:30:200
SSHD_EOF

    mkdir -p /run/sshd
    mkdir -p /code && chmod 777 /code

    apt-get clean
    rm -rf /var/lib/apt/lists/*
fi
