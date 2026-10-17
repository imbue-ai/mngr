from typing import Final

from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.providers.host_access_manifest import HostAccessManifest
from imbue.mngr.providers.host_access_manifest import PerHostDirScheme
from imbue.mngr.providers.host_access_manifest import keypair_file_names
from imbue.mngr_lima.primitives import LimaDiskSize

LIMA_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("lima")

# The key-file names under a VM's own dir, ``keys/hosts/<host id>/``: the
# pre-injected sshd host key, and the root client key mngr uses to reach the VM
# when is_run_as_root is set (the same name directly under ``keys/`` is the
# legacy provider-wide pair older VMs authorize). Pins live per host, next to
# the keys, because localhost ports churn and get reused across VMs.
LIMA_HOST_KEY_NAME: Final[str] = "ssh_host_ed25519_key"
LIMA_ROOT_SSH_KEY_NAME: Final[str] = "root_ssh_key"
LIMA_KNOWN_HOSTS_NAME: Final[str] = "known_hosts"
LIMA_KEY_DIR_NAME: Final[str] = "keys"

# The per-host layout contract, published for consumers that carry a VM's
# access material to another machine.
LIMA_HOST_ACCESS_MANIFEST: Final[HostAccessManifest] = HostAccessManifest(
    key_dir_name=LIMA_KEY_DIR_NAME,
    per_host_dir_scheme=PerHostDirScheme.HOSTS_HOST_ID,
    file_names=keypair_file_names(LIMA_HOST_KEY_NAME, LIMA_ROOT_SSH_KEY_NAME),
    agent_key_file_name=LIMA_ROOT_SSH_KEY_NAME,
    pin_store_names=(LIMA_KNOWN_HOSTS_NAME,),
    is_pin_store_per_host=True,
    known_hosts_link_store_name=None,
)

LIMA_INSTANCE_PREFIX: Final[str] = "mngr-"

# Minimum supported Lima version (major, minor, patch)
MINIMUM_LIMA_VERSION: Final[tuple[int, int, int]] = (1, 0, 0)

# Default image URLs for Lima VMs (Debian 13 "trixie" genericcloud images).
# Debian's genericcloud variant is a minimal cloud image; the provisioning
# script is apt-based, so it works on Debian unchanged and installs any missing
# mngr dependencies. Pinned to a specific dated release for reproducibility;
# bump the release id (in both URLs) to pick up a newer point release.
#
# Served from imbue's public artifact mirror rather than cloud.debian.org, which
# prunes old releases and would eventually break every create pinned to one.
# The mirror holds each release under artifacts/debian-cloud-image/<release>/;
# the ``default_image_url_*`` provider config fields override these for anyone
# who wants another source.
DEFAULT_IMAGE_URL_AARCH64: Final[str] = (
    "https://apt.imbuepackages.com/artifacts/debian-cloud-image/20260722-2547/debian-13-genericcloud-arm64-20260722-2547.qcow2"
)
DEFAULT_IMAGE_URL_X86_64: Final[str] = (
    "https://apt.imbuepackages.com/artifacts/debian-cloud-image/20260722-2547/debian-13-genericcloud-amd64-20260722-2547.qcow2"
)

# Default host directory inside the VM
DEFAULT_HOST_DIR: Final[str] = "/mngr"

# SSH connection timeout when waiting for Lima VM to become reachable
SSH_CONNECT_TIMEOUT_SECONDS: Final[float] = 120.0

# cloud-init completion timeout
CLOUD_INIT_TIMEOUT_SECONDS: Final[float] = 300.0

# Default logical size of the btrfs additional disk. qcow2 is sparse so this
# is a logical cap visible to the guest, not upfront host disk usage.
DEFAULT_HOST_DATA_DISK_SIZE: Final[LimaDiskSize] = LimaDiskSize("100GiB")


def lima_host_data_disk_mount_path(disk_name: str) -> str:
    """Return the in-VM path Lima auto-mounts an additional disk at.

    Lima's additionalDisks machinery generates a systemd mount unit that
    mounts each named disk under ``/mnt/lima-<disk_name>``. This is where
    host_dir symlinks in btrfs mode; no separate canonical bind-mount path
    is introduced, since adding one stacked an empty-ext4 layer underneath
    the btrfs mount when the bind unit fired before Lima's auto-mount.
    """
    return f"/mnt/lima-{disk_name}"


def lima_host_data_disk_label(disk_name: str) -> str:
    """Return the btrfs filesystem label Lima expects on an additional disk.

    Lima's ``05-lima-disks.sh`` boot script formats ``format: true`` disks with
    ``-L lima-<disk_name>`` and treats the presence of
    ``/dev/disk/by-label/lima-<disk_name>`` as "already formatted". mngr's
    in-guest format applies the same label so later boots take Lima's
    already-formatted path instead of re-entering first-time setup.
    """
    return f"lima-{disk_name}"


def lima_host_data_disk_name(host_id: HostId) -> str:
    """Return the Lima `additionalDisks` name for a host's btrfs data volume.

    Embeds the mngr `HostId` so the disk under `~/.lima/_disks/` is unique
    across every mngr-managed Lima host and can be cross-referenced via
    `limactl disk list`.
    """
    return f"mngr-{host_id.get_uuid().hex}-data"
