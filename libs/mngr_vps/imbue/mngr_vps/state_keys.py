from typing import Final

from imbue.mngr.primitives import HostId

# Object-key layout in a provider state bucket, per host. The full host record
# lives at ``hosts/<host_id_hex>/host_state.json`` and each agent's record under
# ``hosts/<host_id_hex>/agents/<agent_id>.json``. ``<host_id_hex>`` matches the
# per-host btrfs subvolume naming (``host_id.get_uuid().hex``) so the same id
# keys both the on-instance volume and the bucket. Shared verbatim by every
# bucket-backed provider (AWS S3, Azure Blob, ...).
HOSTS_PREFIX: Final[str] = "hosts"
HOST_STATE_FILENAME: Final[str] = "host_state.json"
AGENTS_SUBPREFIX: Final[str] = "agents"
# The operator-driven capture at ``mngr stop`` uploads the host's host_dir mirror
# under this subprefix of the host's prefix, i.e. ``hosts/<host_id_hex>/host_dir/...``.
# The offline-read volume is scoped here so reads see exactly the host_dir tree.
HOST_DIR_SUBPREFIX: Final[str] = "host_dir"

# Tag/label marking a cloud resource (state bucket, host identity) as mngr-managed
# so cleanup can prove ownership. Shared verbatim by every provider.
MANAGED_BY_TAG_KEY: Final[str] = "managed-by"
MANAGED_BY_TAG_VALUE: Final[str] = "mngr"


def host_prefix(host_id: HostId) -> str:
    """Return ``hosts/<host_id_hex>`` -- the per-host key prefix (no trailing slash)."""
    return f"{HOSTS_PREFIX}/{host_id.get_uuid().hex}"


def host_state_key(host_id: HostId) -> str:
    """Return the object key for the host's full record."""
    return f"{host_prefix(host_id)}/{HOST_STATE_FILENAME}"


def agent_key(host_id: HostId, agent_id: str) -> str:
    """Return the object key for a single agent's record."""
    return f"{host_prefix(host_id)}/{AGENTS_SUBPREFIX}/{agent_id}.json"


def agents_prefix(host_id: HostId) -> str:
    """Return ``hosts/<host_id_hex>/agents/`` -- the listing prefix for the host's agent records."""
    return f"{host_prefix(host_id)}/{AGENTS_SUBPREFIX}/"


def host_dir_prefix(host_id: HostId) -> str:
    """Return ``hosts/<host_id_hex>/host_dir/`` -- the prefix the operator-driven capture uploads to."""
    return f"{host_prefix(host_id)}/{HOST_DIR_SUBPREFIX}/"
