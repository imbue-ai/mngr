#!/bin/bash
# Provision script for the mngr Lima base image.
# Installs all packages required by mngr hosts.
set -euo pipefail

sudo apt-get update -qq
sudo apt-get install -y -qq --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    openssh-server \
    rsync \
    tmux \
    xxd

# Create sshd run directory
sudo mkdir -p /run/sshd

# Clean up apt caches to reduce image size
sudo apt-get clean
sudo rm -rf /var/lib/apt/lists/*
