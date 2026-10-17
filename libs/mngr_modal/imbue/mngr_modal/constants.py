from typing import Final

from imbue.mngr.providers.host_access_manifest import HostAccessManifest
from imbue.mngr.providers.host_access_manifest import PerHostDirScheme
from imbue.mngr.providers.host_access_manifest import keypair_file_names

# The key-file names under a provider instance's ``keys/`` dir: the client key
# that opens a sandbox and the sandbox sshd's host key, both minted per host
# under ``keys/host_keys/<host uuid hex>/``, and the provider-wide pin store.
MODAL_SSH_KEY_NAME: Final[str] = "modal_ssh_key"
MODAL_HOST_KEY_NAME: Final[str] = "host_key"
MODAL_KNOWN_HOSTS_NAME: Final[str] = "known_hosts"
MODAL_KEY_DIR_NAME: Final[str] = "keys"

# The per-host layout contract (README, "SSH key material"), the same shape as
# the vps providers', published for consumers that carry a host's access
# material to another machine.
MODAL_HOST_ACCESS_MANIFEST: Final[HostAccessManifest] = HostAccessManifest(
    key_dir_name=MODAL_KEY_DIR_NAME,
    per_host_dir_scheme=PerHostDirScheme.HOST_KEYS_UUID_HEX,
    file_names=keypair_file_names(MODAL_SSH_KEY_NAME, MODAL_HOST_KEY_NAME),
    agent_key_file_name=MODAL_SSH_KEY_NAME,
    pin_store_names=(MODAL_KNOWN_HOSTS_NAME,),
    is_pin_store_per_host=False,
    known_hosts_link_store_name=MODAL_KNOWN_HOSTS_NAME,
)

# Prefix for Modal test apps - all test Modal apps should use this prefix
# to enable easy identification and cleanup
MODAL_TEST_APP_PREFIX: Final[str] = "mngr-test-"
