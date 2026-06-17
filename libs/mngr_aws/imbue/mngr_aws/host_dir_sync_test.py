"""Unit tests for the pure host_dir-sync daemon unit/script body generators."""

from imbue.mngr.primitives import HostId
from imbue.mngr_aws.backend import _build_awscli_install_command
from imbue.mngr_aws.backend import _build_host_dir_sync_command
from imbue.mngr_aws.backend import _build_host_dir_sync_service_unit
from imbue.mngr_aws.state_bucket import host_dir_sync_target_for
from imbue.mngr_vps.instance import HOST_DIR_SYNC_INTERVAL_SECONDS
from imbue.mngr_vps.instance import HOST_DIR_SYNC_UNIT_NAME
from imbue.mngr_vps.instance import build_host_dir_sync_timer_unit

_HOST_DIR = "/mnt/mngr-btrfs/abc123/host_dir"
_TARGET = "s3://mngr-state-bucket/hosts/abc123/host_dir/"


def test_sync_command_uses_delete_and_excludes() -> None:
    command = _build_host_dir_sync_command(_HOST_DIR, _TARGET)
    assert command.startswith(f'aws s3 sync "{_HOST_DIR}/" "{_TARGET}" --delete')
    # The known transient-cache excludes are present.
    assert '--exclude "*/__pycache__/*"' in command
    assert '--exclude "*/node_modules/*"' in command


def test_service_unit_is_oneshot_and_runs_the_sync() -> None:
    unit = _build_host_dir_sync_service_unit(_HOST_DIR, _TARGET)
    assert "Type=oneshot" in unit
    assert "ExecStart=/bin/sh -c 'aws s3 sync" in unit
    assert _TARGET in unit


def test_timer_unit_fires_at_the_interval() -> None:
    unit = build_host_dir_sync_timer_unit(HOST_DIR_SYNC_INTERVAL_SECONDS)
    assert f"OnUnitActiveSec={HOST_DIR_SYNC_INTERVAL_SECONDS}" in unit
    assert f"OnBootSec={HOST_DIR_SYNC_INTERVAL_SECONDS}" in unit
    assert f"Unit={HOST_DIR_SYNC_UNIT_NAME}.service" in unit
    assert "WantedBy=timers.target" in unit


def test_awscli_install_is_a_guarded_noop() -> None:
    command = _build_awscli_install_command()
    # Only installs when aws is absent (guarded), so a re-run / baked AMI is a no-op.
    assert "command -v aws" in command
    assert "apt-get install -y awscli" in command


def test_sync_target_uri_matches_host_prefix() -> None:
    host_id = HostId.generate()
    hex_id = host_id.get_uuid().hex
    target = host_dir_sync_target_for("mngr-state-bucket", host_id)
    assert target == f"s3://mngr-state-bucket/hosts/{hex_id}/host_dir/"
