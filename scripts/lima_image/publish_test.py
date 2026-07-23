import threading
from pathlib import Path

import pytest

from scripts.lima_image.publish import ObjectStore
from scripts.lima_image.publish import _upload_store


class _RecordingStore(ObjectStore):
    """Records puts, and rendezvouses in exists() so a serial upload cannot pass.

    ``barrier_parties`` workers must be inside ``exists`` at once for any of them to
    return; a serial loop never gets there and trips the barrier timeout instead.
    """

    def __init__(self, present: set[str], barrier_parties: int = 1) -> None:
        self._present = present
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(barrier_parties, timeout=10.0) if barrier_parties > 1 else None
        self.put_keys: list[str] = []

    def exists(self, key: str) -> bool:
        if self._barrier is not None:
            self._barrier.wait()
        return key in self._present

    def put(self, key: str, data: bytes, content_type: str) -> None:
        with self._lock:
            self.put_keys.append(key)

    def get_optional(self, key: str) -> bytes | None:
        return None


def _write_chunks(store_dir: Path, count: int) -> None:
    for index in range(count):
        shard = store_dir / f"{index % 4:04d}"
        shard.mkdir(parents=True, exist_ok=True)
        (shard / f"{index:064x}.cacnk").write_bytes(f"chunk-{index}".encode())


def test_upload_store_is_parallel_and_skips_present_chunks(tmp_path: Path) -> None:
    # An image is tens of thousands of chunks; uploading them one at a time takes
    # hours. The barrier only releases once every worker is inside exists() at the
    # same time, so a serial uploader hangs here rather than passing slowly.
    store_dir = tmp_path / "store"
    _write_chunks(store_dir, 50)

    present = {f"store/0000/{0:064x}.cacnk"}
    store = _RecordingStore(present=present, barrier_parties=50)
    uploaded = _upload_store(store_dir, store)

    assert uploaded == 49, "every absent chunk must be uploaded"
    assert set(store.put_keys) | present == {f"store/{index % 4:04d}/{index:064x}.cacnk" for index in range(50)}, (
        "every chunk must be probed, and the already-present one must not be re-uploaded"
    )


def test_upload_store_propagates_a_failed_chunk(tmp_path: Path) -> None:
    # A chunk that silently fails to upload publishes an image that cannot be reassembled.
    store_dir = tmp_path / "store"
    _write_chunks(store_dir, 1)

    class _FailingStore(_RecordingStore):
        def put(self, key: str, data: bytes, content_type: str) -> None:
            raise RuntimeError("upload rejected")

    with pytest.raises(RuntimeError):
        _upload_store(store_dir, _FailingStore(present=set()))
