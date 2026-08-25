import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from imbue.minds.desktop_client.device_identity import DEVICE_ID_FILENAME
from imbue.minds.desktop_client.device_identity import get_or_create_device_id
from imbue.minds.errors import DeviceIdError
from imbue.minds.primitives import DeviceId


def test_mints_and_persists_a_fresh_device_id(tmp_path: Path) -> None:
    data_dir = tmp_path / "minds"
    mngr_host_dir = tmp_path / "mngr"

    device_id = get_or_create_device_id(data_dir, mngr_host_dir)

    assert DeviceId(device_id) == device_id
    assert (data_dir / DEVICE_ID_FILENAME).read_text() == device_id
    # A second read returns the persisted id rather than minting a new one.
    assert get_or_create_device_id(data_dir, mngr_host_dir) == device_id


def test_adopts_the_legacy_mngr_host_id_and_leaves_the_original(tmp_path: Path) -> None:
    legacy_host_id = DeviceId.generate()
    mngr_host_dir = tmp_path / "mngr"
    mngr_host_dir.mkdir()
    (mngr_host_dir / "host_id").write_text(f"{legacy_host_id}\n")
    data_dir = tmp_path / "minds"

    device_id = get_or_create_device_id(data_dir, mngr_host_dir)

    assert device_id == legacy_host_id
    assert (data_dir / DEVICE_ID_FILENAME).read_text() == legacy_host_id
    assert (mngr_host_dir / "host_id").read_text() == f"{legacy_host_id}\n"


def test_an_existing_minds_device_id_wins_over_the_legacy_file(tmp_path: Path) -> None:
    minds_device_id = DeviceId.generate()
    legacy_host_id = DeviceId.generate()
    data_dir = tmp_path / "minds"
    data_dir.mkdir()
    (data_dir / DEVICE_ID_FILENAME).write_text(minds_device_id)
    mngr_host_dir = tmp_path / "mngr"
    mngr_host_dir.mkdir()
    (mngr_host_dir / "host_id").write_text(legacy_host_id)

    assert get_or_create_device_id(data_dir, mngr_host_dir) == minds_device_id


def test_raises_on_an_invalid_device_id_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "minds"
    data_dir.mkdir()
    (data_dir / DEVICE_ID_FILENAME).write_text("not-a-host-id")

    with pytest.raises(DeviceIdError):
        get_or_create_device_id(data_dir, tmp_path / "mngr")


def test_raises_on_an_invalid_legacy_host_id_file(tmp_path: Path) -> None:
    mngr_host_dir = tmp_path / "mngr"
    mngr_host_dir.mkdir()
    (mngr_host_dir / "host_id").write_text("corrupt")

    with pytest.raises(DeviceIdError):
        get_or_create_device_id(tmp_path / "minds", mngr_host_dir)


def test_concurrent_first_creations_converge_on_a_single_id(tmp_path: Path) -> None:
    """Callers racing on first creation must all return the winner's persisted id."""
    data_dir = tmp_path / "minds"
    mngr_host_dir = tmp_path / "mngr"
    thread_count = 8
    barrier = threading.Barrier(thread_count)

    def create_after_barrier() -> DeviceId:
        barrier.wait()
        return get_or_create_device_id(data_dir, mngr_host_dir)

    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        device_ids = list(executor.map(lambda _: create_after_barrier(), range(thread_count)))

    assert len(set(device_ids)) == 1
    assert (data_dir / DEVICE_ID_FILENAME).read_text() == device_ids[0]
    # The publish step must clean up its temp files, win or lose.
    assert sorted(path.name for path in data_dir.iterdir()) == [DEVICE_ID_FILENAME]


def test_raises_when_the_device_id_path_cannot_be_created(tmp_path: Path) -> None:
    # A directory squatting on the device id path is unreadable as a file and
    # uncreatable as one; this must abort rather than run without identity.
    data_dir = tmp_path / "minds"
    (data_dir / DEVICE_ID_FILENAME).mkdir(parents=True)

    with pytest.raises(DeviceIdError):
        get_or_create_device_id(data_dir, tmp_path / "mngr")


def test_reports_a_dangling_symlink_squatting_on_the_device_id_path(tmp_path: Path) -> None:
    # A dangling symlink is invisible to exists() yet blocks the atomic link
    # publish; it must be reported as a non-regular file, not as a lost race.
    data_dir = tmp_path / "minds"
    data_dir.mkdir()
    (data_dir / DEVICE_ID_FILENAME).symlink_to(data_dir / "missing-target")

    with pytest.raises(DeviceIdError, match="not a regular file"):
        get_or_create_device_id(data_dir, tmp_path / "mngr")
