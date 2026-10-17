from typing import Final

from imbue.mngr.primitives import ProviderBackendName

DOCKER_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("docker")

DOCKER_BUILD_ARGS_HELP: Final[str] = (
    "Build args are passed directly to 'docker build'. Run 'docker build --help' for details."
)

DOCKER_START_ARGS_HELP: Final[str] = (
    "Start args are passed directly to 'docker run'. Run 'docker run --help' for details."
)
