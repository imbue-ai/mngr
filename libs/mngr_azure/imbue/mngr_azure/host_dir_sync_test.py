"""Unit tests for the pure host_dir-sync daemon unit/script body generators."""

from imbue.mngr.primitives import HostId
from imbue.mngr_azure.backend import HOST_DIR_SYNC_INTERVAL_SECONDS
from imbue.mngr_azure.backend import HOST_DIR_SYNC_UNIT_NAME
from imbue.mngr_azure.backend import _build_azcopy_install_command
from imbue.mngr_azure.backend import _build_host_dir_blob_url
from imbue.mngr_azure.backend import _build_host_dir_sync_command
from imbue.mngr_azure.backend import _build_host_dir_sync_service_unit
from imbue.mngr_vps.instance import build_host_dir_sync_timer_unit

_HOST_DIR = "/mnt/mngr-btrfs/abc123/host_dir"
_BLOB_URL = "https://mngrstabc.blob.core.windows.net/mngr-state/hosts/abc123/host_dir"
_CLIENT_ID = "client-1"


def test_sync_command_uses_delete_destination_and_excludes() -> None:
    command = _build_host_dir_sync_command(_HOST_DIR, _BLOB_URL)
    assert command.startswith(f'azcopy sync "{_HOST_DIR}" "{_BLOB_URL}"')
    assert "--delete-destination=true" in command
    # File-name globs go on --exclude-pattern; directory trees go on --exclude-path
    # (azcopy treats a pattern as a file name, so it would not skip the dir trees).
    assert '--exclude-pattern "*.tmp"' in command
    assert '--exclude-path "__pycache__;node_modules"' in command


def test_service_unit_is_oneshot_and_authenticates_as_the_managed_identity() -> None:
    unit = _build_host_dir_sync_service_unit(_HOST_DIR, _BLOB_URL, _CLIENT_ID)
    assert "Type=oneshot" in unit
    assert "ExecStart=/bin/sh -c 'azcopy sync" in unit
    # azcopy authenticates as the VM's user-assigned identity via MSI.
    assert "Environment=AZCOPY_AUTO_LOGIN_TYPE=MSI" in unit
    assert f"Environment=AZCOPY_MSI_CLIENT_ID={_CLIENT_ID}" in unit


def test_timer_unit_fires_at_the_interval() -> None:
    unit = build_host_dir_sync_timer_unit(HOST_DIR_SYNC_INTERVAL_SECONDS)
    assert f"OnUnitActiveSec={HOST_DIR_SYNC_INTERVAL_SECONDS}" in unit
    assert f"OnBootSec={HOST_DIR_SYNC_INTERVAL_SECONDS}" in unit
    assert f"Unit={HOST_DIR_SYNC_UNIT_NAME}.service" in unit
    assert "WantedBy=timers.target" in unit


def test_azcopy_install_is_a_guarded_noop() -> None:
    command = _build_azcopy_install_command()
    # Only installs when azcopy is absent (guarded), so a re-run / baked image is a no-op.
    assert "command -v azcopy" in command
    assert "downloadazcopy-v10-linux" in command


def test_blob_url_matches_host_prefix() -> None:
    host_id = HostId.generate()
    hex_id = host_id.get_uuid().hex
    url = _build_host_dir_blob_url("mngrstabc", "mngr-state", host_id)
    assert url == f"https://mngrstabc.blob.core.windows.net/mngr-state/hosts/{hex_id}/host_dir"
