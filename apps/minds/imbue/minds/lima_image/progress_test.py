from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.minds.lima_image.data_types import LimaImagePrefetchState
from imbue.minds.lima_image.data_types import LimaImagePrefetchStatus
from imbue.minds.lima_image.primitives import ImageArch
from imbue.minds.lima_image.primitives import MindsImageVersion
from imbue.minds.lima_image.progress import FileLimaImageProgressSink


def _state(status: LimaImagePrefetchStatus) -> LimaImagePrefetchState:
    return LimaImagePrefetchState(
        status=status,
        minds_version=MindsImageVersion("minds-v0.3.4"),
        arch=ImageArch.X86_64,
        updated_at=datetime.now(timezone.utc),
    )


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    sink = FileLimaImageProgressSink(state_file=tmp_path / "nested" / "state.json")
    sink.write_state(_state(LimaImagePrefetchStatus.DOWNLOADING))
    read = sink.read_state()
    assert read is not None and read.status is LimaImagePrefetchStatus.DOWNLOADING


def test_read_missing_file_returns_none(tmp_path: Path) -> None:
    assert FileLimaImageProgressSink(state_file=tmp_path / "absent.json").read_state() is None


def test_read_malformed_file_returns_none(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("{not valid json")
    assert FileLimaImageProgressSink(state_file=state_file).read_state() is None


def test_latest_write_wins(tmp_path: Path) -> None:
    sink = FileLimaImageProgressSink(state_file=tmp_path / "state.json")
    sink.write_state(_state(LimaImagePrefetchStatus.FETCHING_MANIFEST))
    sink.write_state(_state(LimaImagePrefetchStatus.READY))
    read = sink.read_state()
    assert read is not None and read.status is LimaImagePrefetchStatus.READY
