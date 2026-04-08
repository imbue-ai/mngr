#!/bin/bash
# Wrapper script that starts the Docker daemon (if not already running)
set -euo pipefail
# and then exec's the given command with all arguments.
# Used by offload-modal-release.toml as the pytest command prefix.
set -eo pipefail

if ! docker info >/dev/null 2>&1 && [ -x /start-dockerd.sh ]; then
    /start-dockerd.sh >/dev/null 2>&1
fi

exec "$@"
