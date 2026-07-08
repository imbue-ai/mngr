"""Unit tests for build_pool_host_wipe_script -- the destroy_host data-wipe generator.

Pure function; we assert on the exact command shape that destroy_host's SSH
transport will execute on the leased VPS.
"""

from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.providers.wipe import build_pool_host_wipe_script

_WIPE_HOST_ID = HostId("host-2e6c6d70f14c4f3cbb14da81f68a5dc9")
_WIPE_HOST_HEX = "2e6c6d70f14c4f3cbb14da81f68a5dc9"


def test_build_pool_host_wipe_script_starts_with_set_plus_e() -> None:
    """Every wipe step is best-effort -- the script must NOT abort on first failure."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    assert script.startswith("set +e\n"), script[:50]


def test_build_pool_host_wipe_script_ends_with_exit_zero() -> None:
    """Caller relies on a clean exit so a single failed wipe doesn't block release."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    assert script.rstrip().endswith("exit 0"), script[-50:]


def test_build_pool_host_wipe_script_filters_container_by_host_id_label() -> None:
    """Container lookup must filter on the canonical mngr_vps host-id label.

    ``shlex.quote`` is a no-op for the alphanumeric-with-dashes HostId so the
    rendered filter is unquoted; that's still valid shell because the value
    contains no spaces or metacharacters.
    """
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    expected_label = f"label=com.imbue.mngr.host-id={_WIPE_HOST_ID}"
    assert f"docker ps -a --filter {expected_label}" in script


def test_build_pool_host_wipe_script_removes_container_with_volumes() -> None:
    """``docker rm -f -v`` to drop anonymous volumes attached to the workspace container."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    assert 'docker rm -f -v "$container_id"' in script


def test_build_pool_host_wipe_script_drops_named_host_volume_by_hex() -> None:
    """The per-host bind-options volume name embeds the host hex; filter by it."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    expected_filter = f"name=mngr-host-vol-{_WIPE_HOST_HEX}"
    assert f"docker volume ls -q --filter {expected_filter}" in script
    assert 'docker volume rm -f "$vol"' in script


def test_build_pool_host_wipe_script_deletes_btrfs_subvolume_with_rm_rf_fallback() -> None:
    """btrfs subvolume delete preferred; falls back to rm -rf for non-btrfs hosts."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    subvol_path = f"/mngr-btrfs/{_WIPE_HOST_HEX}"
    assert f"if [ -d {subvol_path} ]; then" in script
    assert f"btrfs subvolume delete {subvol_path}" in script
    assert f"rm -rf {subvol_path}" in script


def test_build_pool_host_wipe_script_runs_docker_system_prune() -> None:
    """Reclaim everything else docker still holds (stopped containers, dangling images, ...)."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    assert "docker system prune -a -f --volumes" in script


def test_build_pool_host_wipe_script_wipes_root_and_tmp_preserving_authorized_keys() -> None:
    """``/root`` and ``/tmp`` get wiped; ``authorized_keys`` survives so the pool-mgmt key keeps working."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    assert "find /root -mindepth 1 -maxdepth 1 -not -name .ssh -exec rm -rf {} +" in script
    assert "find /root/.ssh -mindepth 1 -not -name authorized_keys -exec rm -rf {} +" in script
    assert "find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} +" in script


def test_build_pool_host_wipe_script_renders_safe_host_id_inline() -> None:
    """``shlex.quote`` is a no-op on the `[a-z0-9-]` HostId shape, so the
    rendered template carries the host id inline (still valid shell since
    the value has no spaces or metacharacters). The point of routing through
    ``shlex.quote`` is to remain safe if the HostId shape ever broadens; the
    test here pins today's expected rendering.
    """
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    assert f"label=com.imbue.mngr.host-id={_WIPE_HOST_ID}" in script
    assert f"name=mngr-host-vol-{_WIPE_HOST_HEX}" in script
