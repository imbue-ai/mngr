#!/bin/bash
# Sourced via BASH_ENV before every bash -c command in the sandbox.
# Starts the Docker daemon if it's not already running. Skips silently
# during image builds (where iptables/dockerd can't work) by checking
# for iptables nat table support as a proxy for runtime capabilities.
if [ -x /start-dockerd.sh ] && ! /usr/local/bin/docker info >/dev/null 2>&1; then
    iptables-legacy -t nat -L >/dev/null 2>&1 && /start-dockerd.sh >/dev/null 2>&1 || true
fi
