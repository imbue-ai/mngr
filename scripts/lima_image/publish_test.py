import threading
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from scripts.lima_image.publish import CloudflareApiObjectStore
from scripts.lima_image.publish import ObjectStore
from scripts.lima_image.publish import _upload_store


class _RecordingStore(ObjectStore):
    """Records puts, and rendezvouses in exists() so a serial upload cannot pass.

    ``barrier_parties`` workers must be inside ``exists`` at once for any of them to
    return; a serial loop deadlocks and trips the barrier timeout instead.
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


_ACCOUNT = "acct123"
_BUCKET = "minds-lima-images-test"
_KEY = "store/0001/abc.cacnk"


def _store(handler: Callable[[httpx.Request], httpx.Response]) -> CloudflareApiObjectStore:
    """A store whose HTTP calls are served by ``handler`` instead of the network."""
    return CloudflareApiObjectStore(
        account_id=_ACCOUNT,
        api_token="tok",
        bucket=_BUCKET,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_exists_uses_get_because_the_object_api_rejects_head() -> None:
    # Cloudflare's R2 object API answers HEAD with 405, so an exists() built on HEAD
    # raises on the very first chunk and no upload can ever succeed.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(404)

    assert _store(handler).exists(_KEY) is False
    assert seen == ["GET"], f"exists() must probe with GET, not {seen}"


def test_exists_is_true_for_a_present_object() -> None:
    assert _store(lambda request: httpx.Response(200, content=b"chunk")).exists(_KEY) is True


def test_exists_raises_on_auth_failure_rather_than_reporting_absent() -> None:
    # Reading a 403 as "absent" makes a store that cannot be read look merely empty:
    # the publish re-uploads all 65k chunks instead of surfacing the bad token.
    with pytest.raises(httpx.HTTPStatusError):
        _store(lambda request: httpx.Response(403)).exists(_KEY)


def test_get_optional_returns_none_only_for_a_missing_object() -> None:
    assert _store(lambda request: httpx.Response(404)).get_optional(_KEY) is None
    assert _store(lambda request: httpx.Response(200, content=b"body")).get_optional(_KEY) == b"body"


def test_get_optional_raises_on_auth_failure() -> None:
    # A 403 read of the root manifest must not look like "no manifest published yet":
    # the publisher would overwrite the manifest and drop the other arch's entry.
    with pytest.raises(httpx.HTTPStatusError):
        _store(lambda request: httpx.Response(403)).get_optional(_KEY)


def test_put_targets_the_object_api_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    _store(handler).put(_KEY, b"data", "application/octet-stream")
    (request,) = seen
    assert request.method == "PUT"
    assert str(request.url).endswith(f"/accounts/{_ACCOUNT}/r2/buckets/{_BUCKET}/objects/{_KEY}")
    assert request.headers["content-type"] == "application/octet-stream"


def test_upload_store_is_parallel_and_skips_present_chunks(tmp_path: Path) -> None:
    # A multi-GB image is tens of thousands of chunks; a serial upload takes hours.
    store_dir = tmp_path / "store"
    for i in range(50):
        shard = store_dir / f"{i % 4:04d}"
        shard.mkdir(parents=True, exist_ok=True)
        (shard / f"{i:064x}.cacnk").write_bytes(f"chunk-{i}".encode())

    # Every chunk must be probed concurrently: the barrier only releases once all 50
    # workers are inside exists() at once, so a serial upload times out here.
    present = {f"store/0000/{0:064x}.cacnk"}
    store = _RecordingStore(present=present, barrier_parties=50)
    uploaded = _upload_store(store_dir, store)

    assert uploaded == 49, "every absent chunk must be uploaded"
    assert set(store.put_keys) | present == {f"store/{i % 4:04d}/{i:064x}.cacnk" for i in range(50)}, (
        "every chunk must be probed and uploaded exactly once"
    )


def test_upload_store_propagates_a_failed_chunk(tmp_path: Path) -> None:
    # A silently dropped chunk publishes an image that cannot be reassembled.
    store_dir = tmp_path / "store" / "0000"
    store_dir.mkdir(parents=True)
    (store_dir / f"{1:064x}.cacnk").write_bytes(b"data")

    class _FailingStore(_RecordingStore):
        def put(self, key: str, data: bytes, content_type: str) -> None:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    with pytest.raises(httpx.HTTPStatusError):
        _upload_store(tmp_path / "store", _FailingStore(present=set()))
