#!/bin/bash
# Wrapper script that starts the Docker daemon (if not already running)
# and then exec's the given command with all arguments.
# Available in release images as a command prefix for scripts that need Docker.
set -euo pipefail

if ! docker info >/dev/null 2>&1 && [ -x /start-dockerd.sh ]; then
    /start-dockerd.sh >/dev/null 2>&1
fi

exec "$@"
