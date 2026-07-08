import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.hosts.offline_host import OfflineHostWithVolume
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.providers.docker.host_store import HostRecord
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.testing import make_docker_provider_with_local_volume
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr_file.cli.put import _emit_put_result

_HOST_ID = "host-00000000000000000000000000000002"


def test_emit_put_result_human_writes_message(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template=None)

    _emit_put_result(Path("/test/file.txt"), 1024, output_opts)

    captured = capsys.readouterr()
    assert "1024" in captured.out
    assert "/test/file.txt" in captured.out


def test_emit_put_result_json_emits_event(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.JSON, format_template=None)

    _emit_put_result(Path("/test/file.txt"), 512, output_opts)

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["event"] == "file_written"
    assert parsed["size"] == 512
    assert parsed["path"] == "/test/file.txt"


def test_emit_put_result_jsonl_emits_event(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.JSONL, format_template=None)

    _emit_put_result(Path("/test/file.txt"), 256, output_opts)

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["event"] == "file_written"
    assert parsed["size"] == 256


# --- offline (volume-backed) put: the branch file_put delegates to ---


def _make_readable_offline_host(provider: DockerProviderInstance, host_id: HostId) -> OfflineHostWithVolume:
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="h",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    host = provider._create_host_from_host_record(record)
    assert isinstance(host, OfflineHostWithVolume)
    return host


def _host_volume_root(provider: DockerProviderInstance, host_id: HostId, volume_root: Path) -> Path:
    """Return the on-disk directory backing the host's volume (host_dir root)."""
    vol_id = DockerProviderInstance._volume_id_for_host(host_id)
    root = volume_root / "volumes" / str(vol_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_offline_put_writes_to_volume_and_ignores_mode(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """Writing to a stopped (volume-backed) host lands on the volume; --mode is ignored.

    This exercises the offline branch of ``mngr file put``: ``file_put`` resolves a
    volume-backed host and calls ``host.write_file(full_path, content, mode=opts.mode)``.
    The write must reach the persisted volume (creating parents), be readable back
    through the same host interface, and ``--mode`` -- which a volume write cannot
    apply -- must be ignored with a warning rather than raising.
    """
    host_id = HostId(_HOST_ID)
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    host = _make_readable_offline_host(provider, host_id)

    root = _host_volume_root(provider, host_id, tmp_path)
    target_path = host.host_dir / "staged" / "f.txt"

    with capture_loguru() as logs:
        host.write_file(target_path, b"payload\n", mode="0755")

    # The bytes landed on the persisted volume at the addressed path (parents created).
    assert (root / "staged" / "f.txt").read_bytes() == b"payload\n"
    # And read back through the same host interface.
    assert host.read_file(target_path) == b"payload\n"
    # --mode is not settable on a volume write and is ignored (with a warning).
    assert "mode is not settable" in logs.getvalue()
