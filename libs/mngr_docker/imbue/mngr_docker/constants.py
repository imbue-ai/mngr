from typing import Final

from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.providers.host_access_manifest import HostAccessManifest
from imbue.mngr.providers.host_access_manifest import PerHostDirScheme
from imbue.mngr.providers.host_access_manifest import keypair_file_names

DOCKER_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("docker")

# The key-file names under a provider instance's ``keys/`` dir: the client key
# that opens a container and the container sshd's host key, both minted per
# host under ``keys/host_keys/<host uuid hex>/``, and the provider-wide pin store.
DOCKER_SSH_KEY_NAME: Final[str] = "docker_ssh_key"
DOCKER_HOST_KEY_NAME: Final[str] = "host_key"
DOCKER_KNOWN_HOSTS_NAME: Final[str] = "known_hosts"
DOCKER_KEY_DIR_NAME: Final[str] = "keys"

# The per-host layout contract, the same shape as modal's, published for
# consumers that carry a host's access material to another machine.
DOCKER_HOST_ACCESS_MANIFEST: Final[HostAccessManifest] = HostAccessManifest(
    key_dir_name=DOCKER_KEY_DIR_NAME,
    per_host_dir_scheme=PerHostDirScheme.HOST_KEYS_UUID_HEX,
    file_names=keypair_file_names(DOCKER_SSH_KEY_NAME, DOCKER_HOST_KEY_NAME),
    agent_key_file_name=DOCKER_SSH_KEY_NAME,
    pin_store_names=(DOCKER_KNOWN_HOSTS_NAME,),
    is_pin_store_per_host=False,
    known_hosts_link_store_name=DOCKER_KNOWN_HOSTS_NAME,
)

DOCKER_BUILD_ARGS_HELP: Final[str] = (
    "Build args are passed directly to 'docker build'. Run 'docker build --help' for details."
)

DOCKER_START_ARGS_HELP: Final[str] = (
    "Start args are passed directly to 'docker run'. Run 'docker run --help' for details."
)
