#!/bin/bash
# Sourced via BASH_ENV before every bash -c command in the sandbox.
# Starts the Docker daemon if it's not already running. Skips silently
# during image builds (where iptables/dockerd can't work) by checking
# for iptables nat table support as a proxy for runtime capabilities.
#
# NOTE: This script intentionally omits 'set -euo pipefail' because it
# is sourced via BASH_ENV into every bash session. Strict mode would
# propagate to the parent shell and break commands that reference unset
# variables or have expected non-zero exit codes.
if [ -x /start-dockerd.sh ] && ! /usr/local/bin/docker info >/dev/null 2>&1; then
    # Serialize concurrent bash subshells so only one runs start-dockerd.sh;
    # others wait here and re-check `docker info` once the holder releases
    # the lock. Without this, every concurrent bash -c can spawn a fresh
    # start-dockerd.sh because none of start-dockerd.sh's internal guards
    # trigger before the siblings pass the outer check.
    iptables-legacy -t nat -L >/dev/null 2>&1 && \
        flock /var/run/ensure-dockerd.lock -c \
        '/usr/local/bin/docker info >/dev/null 2>&1 || /start-dockerd.sh >/dev/null 2>&1' \
        || true
fi
