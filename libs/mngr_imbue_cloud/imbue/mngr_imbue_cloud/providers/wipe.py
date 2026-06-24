import shlex
from typing import Final

from imbue.mngr.primitives import HostId

# Path on every mngr_vps-baked outer where the per-host btrfs loop
# filesystem is mounted (matches ``VpsProviderConfig.btrfs_mount_path``
# default). The per-host subvolume is at ``<this>/<host_id_hex>``.
_VPS_BTRFS_MOUNT_PATH: Final[str] = "/mngr-btrfs"

# Container label that mngr_vps tags the workspace container with;
# also used as ``docker volume`` -name search prefix (the bind-options volume
# is named ``mngr-host-vol-<host_id_hex>``).
_LABEL_HOST_ID_KEY: Final[str] = "com.imbue.mngr.host-id"
_HOST_VOLUME_NAME_PREFIX: Final[str] = "mngr-host-vol-"


def build_pool_host_wipe_script(host_id: HostId) -> str:
    """Render the bash script that wipes a leased pool VPS before release.

    Runs as root on the outer (the leased VPS itself). Each step is
    independently best-effort -- the script always exits 0 so a single
    failed sub-step (e.g. ``docker stop`` on an already-stopped container)
    doesn't abort the rest. The destroy-host caller still logs warnings
    when this returns abnormally, but the privacy-relevant steps remain
    unconditionally attempted.

    Wipes:

    * The workspace container (by label) and any named docker volume
      whose name contains the host hex.
    * The per-host btrfs subvolume backing the bind-options volume (the
      bind source ``device=`` lives outside the container, so removing
      the container alone leaves the data on disk).
    * Everything docker still knows about (``system prune -a -f --volumes``).
    * Everything under ``/root`` and ``/tmp`` except
      ``/root/.ssh/authorized_keys`` (preserves the static pool-management
      key the connector / cleanup_released_hosts.py needs for any
      post-release SSH-driven maintenance).

    Pure function so unit tests can assert the exact command shape
    without standing up an SSH transport.
    """
    host_id_hex = host_id.get_uuid().hex
    host_id_str = str(host_id)
    label_filter = shlex.quote(f"label={_LABEL_HOST_ID_KEY}={host_id_str}")
    volume_name_filter = shlex.quote(f"name={_HOST_VOLUME_NAME_PREFIX}{host_id_hex}")
    subvolume_path = f"{_VPS_BTRFS_MOUNT_PATH}/{host_id_hex}"
    return (
        "set +e\n"
        # Find + remove the workspace container by its host-id label.
        f"container_id=$(docker ps -a --filter {label_filter} --format '{{{{.ID}}}}' | head -1)\n"
        'if [ -n "$container_id" ]; then\n'
        '    docker stop "$container_id" >/dev/null 2>&1\n'
        '    docker rm -f -v "$container_id" >/dev/null 2>&1\n'
        "fi\n"
        # Drop the per-host named volume(s). docker volume rm -f tolerates
        # the volume not existing.
        f"for vol in $(docker volume ls -q --filter {volume_name_filter}); do\n"
        '    docker volume rm -f "$vol" >/dev/null 2>&1\n'
        "done\n"
        # The bind-options volume's storage is a btrfs subvolume at
        # /mngr-btrfs/<host_hex>; docker volume rm doesn't touch the bind
        # source, so wipe it explicitly. Fall back to rm -rf if btrfs is
        # absent (older non-btrfs hosts before the btrfs spec landed).
        f"if [ -d {shlex.quote(subvolume_path)} ]; then\n"
        f"    btrfs subvolume delete {shlex.quote(subvolume_path)} >/dev/null 2>&1 || "
        f"rm -rf {shlex.quote(subvolume_path)} >/dev/null 2>&1\n"
        "fi\n"
        # Reclaim everything else docker holds: stopped containers, dangling
        # images, networks, unused volumes, build cache.
        "docker system prune -a -f --volumes >/dev/null 2>&1\n"
        # Wipe /root content except .ssh/authorized_keys -- preserves the
        # pool-management public key the connector relies on if it needs
        # to reach the host again before cleanup_released_hosts.py runs.
        "find /root -mindepth 1 -maxdepth 1 -not -name .ssh -exec rm -rf {} + 2>/dev/null\n"
        "find /root/.ssh -mindepth 1 -not -name authorized_keys -exec rm -rf {} + 2>/dev/null\n"
        # Wipe /tmp entirely.
        "find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null\n"
        # Always succeed: release is the gating step, individual wipe
        # failures are surfaced via the script's stderr but must not
        # block lease return.
        "exit 0\n"
    )
