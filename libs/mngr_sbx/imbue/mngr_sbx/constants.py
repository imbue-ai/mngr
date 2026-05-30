from typing import Final

from imbue.mngr.primitives import ProviderBackendName

SBX_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("sbx")

# Default host directory mounted inside the sandbox.
DEFAULT_HOST_DIR: Final[str] = "/mngr"

# Default agent type used when creating an sbx sandbox. "docker-agent" is the
# most generic option; the mngr provider treats the sbx sandbox as a host and
# installs its own sshd inside, rather than relying on the sbx-managed agent
# process.
DEFAULT_SBX_AGENT_TYPE: Final[str] = "docker-agent"

# Timeout for waiting for sshd to be reachable inside a sandbox.
SSH_CONNECT_TIMEOUT_SECONDS: Final[float] = 90.0

# Maximum time to wait for "sbx login" credentials to load on first call.
SBX_AUTH_PROBE_TIMEOUT_SECONDS: Final[float] = 10.0
