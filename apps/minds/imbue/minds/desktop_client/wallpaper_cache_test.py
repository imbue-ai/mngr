import json
import threading
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.wallpaper_cache import FetchedWallpaper
from imbue.minds.desktop_client.wallpaper_cache import MAX_WALLPAPER_BYTES
from imbue.minds.desktop_client.wallpaper_cache import WallpaperCache
from imbue.minds.desktop_client.wallpaper_cache import WallpaperRefresher
from imbue.minds.desktop_client.wallpaper_cache import content_version
from imbue.minds.desktop_client.wallpaper_cache import fetch_workspace_wallpaper
from imbue.minds.desktop_client.wallpaper_cache import parse_inventory_wallpaper
from imbue.minds.errors import WallpaperFetchError
from imbue.mngr.utils.polling import wait_for

_WORKSPACE_ID = "agent-" + "c" * 32
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _inventory(wallpaper: object) -> dict[str, object]:
    return {"workspace_name": "alpha", "desktops": [{"id": "home", "wallpaper": wallpaper}], "apps": []}


def test_parse_inventory_wallpaper_reads_the_first_desktops_wallpaper_and_none_for_the_theme_default() -> None:
    assert parse_inventory_wallpaper(_inventory({"kind": "file", "name": "dunes"})) == ("file", "dunes")
    assert parse_inventory_wallpaper(_inventory(None)) is None
    assert parse_inventory_wallpaper({"desktops": []}) is None


@pytest.mark.parametrize(
    "inventory",
    [
        [],
        {"desktops": [None]},
        {"desktops": [{"wallpaper": "arcs"}]},
        {"desktops": [{"wallpaper": {"kind": "bundled"}}]},
        {"desktops": [{"wallpaper": {"kind": "", "name": "arcs"}}]},
    ],
)
def test_parse_inventory_wallpaper_refuses_a_malformed_document(inventory: object) -> None:
    with pytest.raises(WallpaperFetchError):
        parse_inventory_wallpaper(inventory)


def _shell(
    inventory_status: int = 200,
    wallpaper: object = None,
    image_status: int = 200,
    image_type: str = "image/png",
    image_body: bytes = _PNG,
) -> httpx2.Client:
    """A fake workspace shell behind the forward: the inventory, and the one wallpaper it names."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["host"] == f"{_WORKSPACE_ID}.localhost"
        if request.url.path == "/api/inventory":
            return httpx2.Response(inventory_status, json=_inventory(wallpaper))
        if request.url.path.startswith("/wallpapers/"):
            return httpx2.Response(image_status, content=image_body, headers={"content-type": image_type})
        return httpx2.Response(404)

    return httpx2.Client(transport=httpx2.MockTransport(handle))


def test_fetch_workspace_wallpaper_reads_the_named_image_through_the_forward() -> None:
    with _shell(wallpaper={"kind": "file", "name": "dunes"}) as client:
        fetched = fetch_workspace_wallpaper(client, 8421, _WORKSPACE_ID)

    assert fetched == FetchedWallpaper(kind="file", name="dunes", content_type="image/png", body=_PNG)


def test_fetch_workspace_wallpaper_encodes_the_wallpapers_kind_and_name_into_the_path() -> None:
    paths: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/inventory":
            return httpx2.Response(200, json=_inventory({"kind": "file", "name": "dunes/../x?y#z"}))
        paths.append(request.url.raw_path.decode())
        return httpx2.Response(200, content=_PNG, headers={"content-type": "image/png"})

    with httpx2.Client(transport=httpx2.MockTransport(handle)) as client:
        fetched = fetch_workspace_wallpaper(client, 8421, _WORKSPACE_ID)

    assert fetched is not None and fetched.name == "dunes/../x?y#z"
    assert paths == ["/wallpapers/file/dunes%2F..%2Fx%3Fy%23z"]


def test_fetch_workspace_wallpaper_answers_none_for_the_theme_default() -> None:
    with _shell(wallpaper=None) as client:
        assert fetch_workspace_wallpaper(client, 8421, _WORKSPACE_ID) is None


@pytest.mark.parametrize(
    ("inventory_status", "image_status", "image_type", "image_body"),
    [
        (503, 200, "image/png", _PNG),
        (200, 404, "image/png", _PNG),
        (200, 200, "text/html", _PNG),
        (200, 200, "image/png", b"x" * (MAX_WALLPAPER_BYTES + 1)),
    ],
    ids=["inventory_refused", "image_missing", "not_an_image", "too_large"],
)
def test_fetch_workspace_wallpaper_refuses_a_missing_or_unacceptable_answer(
    inventory_status: int, image_status: int, image_type: str, image_body: bytes
) -> None:
    shell = _shell(
        inventory_status=inventory_status,
        wallpaper={"kind": "bundled", "name": "arcs"},
        image_status=image_status,
        image_type=image_type,
        image_body=image_body,
    )
    with shell as client:
        with pytest.raises(WallpaperFetchError):
            fetch_workspace_wallpaper(client, 8421, _WORKSPACE_ID)


def test_fetch_workspace_wallpaper_stops_reading_an_oversized_body_at_the_limit() -> None:
    chunk = b"x" * (1024 * 1024)
    served_chunks = 0

    def stream_chunks() -> Iterator[bytes]:
        nonlocal served_chunks
        for _ in range(MAX_WALLPAPER_BYTES // len(chunk) + 8):
            served_chunks += 1
            yield chunk

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/inventory":
            return httpx2.Response(200, json=_inventory({"kind": "bundled", "name": "arcs"}))
        return httpx2.Response(200, stream=_ChunkStream(stream_chunks()), headers={"content-type": "image/png"})

    with httpx2.Client(transport=httpx2.MockTransport(handle)) as client:
        with pytest.raises(WallpaperFetchError):
            fetch_workspace_wallpaper(client, 8421, _WORKSPACE_ID)

    # The read stopped as soon as the limit was passed, not at the end of the body.
    assert served_chunks <= MAX_WALLPAPER_BYTES // len(chunk) + 1


class _ChunkStream(httpx2.SyncByteStream):
    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        return self._chunks


def test_fetch_workspace_wallpaper_refuses_a_shell_that_does_not_answer() -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with httpx2.Client(transport=httpx2.MockTransport(fail)) as client:
        with pytest.raises(WallpaperFetchError):
            fetch_workspace_wallpaper(client, 8421, _WORKSPACE_ID)


def test_cache_stores_an_image_and_serves_it_back_under_its_version(tmp_path: Path) -> None:
    changes: list[int] = []
    cache = WallpaperCache(cache_dir=tmp_path / "wallpapers", on_change=lambda: changes.append(1))
    assert cache.version_for(_WORKSPACE_ID) == ""
    assert cache.read_image(_WORKSPACE_ID) is None

    cache.store(_WORKSPACE_ID, FetchedWallpaper(kind="file", name="dunes", content_type="image/png", body=_PNG))

    image = cache.read_image(_WORKSPACE_ID)
    assert image is not None
    assert (image.content_type, image.body) == ("image/png", _PNG)
    assert image.version == cache.version_for(_WORKSPACE_ID) == content_version(_PNG)
    assert changes == [1]

    # A fresh cache over the same directory reads the record and the image back.
    reloaded = WallpaperCache(cache_dir=tmp_path / "wallpapers")
    assert reloaded.version_for(_WORKSPACE_ID) == content_version(_PNG)
    reloaded_image = reloaded.read_image(_WORKSPACE_ID)
    assert reloaded_image is not None and reloaded_image.body == _PNG


def test_cache_records_no_wallpaper_as_an_empty_version_and_drops_the_old_image(tmp_path: Path) -> None:
    cache = WallpaperCache(cache_dir=tmp_path / "wallpapers")
    cache.store(_WORKSPACE_ID, FetchedWallpaper(kind="file", name="dunes", content_type="image/png", body=_PNG))

    cache.store(_WORKSPACE_ID, None)

    assert cache.version_for(_WORKSPACE_ID) == ""
    assert cache.read_image(_WORKSPACE_ID) is None
    assert not (tmp_path / "wallpapers" / f"{_WORKSPACE_ID}.img").exists()
    record = json.loads((tmp_path / "wallpapers" / f"{_WORKSPACE_ID}.json").read_text())
    assert (record["kind"], record["name"], record["version"]) == (None, None, "")


def test_cache_skips_an_unreadable_record_and_keeps_the_others(tmp_path: Path) -> None:
    cache_dir = tmp_path / "wallpapers"
    seeded = WallpaperCache(cache_dir=cache_dir)
    seeded.store(_WORKSPACE_ID, FetchedWallpaper(kind="file", name="dunes", content_type="image/png", body=_PNG))
    (cache_dir / "agent-broken.json").write_text("{not json")

    cache = WallpaperCache(cache_dir=cache_dir)

    assert cache.version_for(_WORKSPACE_ID) == content_version(_PNG)
    assert cache.version_for("agent-broken") == ""


def test_refresher_fetches_on_a_thread_and_writes_the_cache_once_per_request(tmp_path: Path) -> None:
    stored = threading.Event()
    cache = WallpaperCache(cache_dir=tmp_path / "wallpapers", on_change=stored.set)
    with ConcurrencyGroup(name=f"wallpaper-refresh-{uuid4().hex}") as cg:
        refresher = WallpaperRefresher(
            cache=cache,
            mngr_forward_port=8421,
            make_probe_client=lambda: _shell(wallpaper={"kind": "bundled", "name": "arcs"}),
            concurrency_group=cg,
        )

        assert refresher.request_refresh(_WORKSPACE_ID) is True
        assert stored.wait(timeout=10)

    assert cache.version_for(_WORKSPACE_ID) == content_version(_PNG)


def test_refresher_keeps_the_cache_when_the_shell_refuses(tmp_path: Path) -> None:
    cache = WallpaperCache(cache_dir=tmp_path / "wallpapers")
    cache.store(_WORKSPACE_ID, FetchedWallpaper(kind="file", name="dunes", content_type="image/png", body=_PNG))
    finished = threading.Event()

    def failing_client() -> httpx2.Client:
        client = _shell(inventory_status=503)
        finished.set()
        return client

    with ConcurrencyGroup(name=f"wallpaper-refresh-{uuid4().hex}") as cg:
        refresher = WallpaperRefresher(
            cache=cache, mngr_forward_port=8421, make_probe_client=failing_client, concurrency_group=cg
        )
        assert refresher.request_refresh(_WORKSPACE_ID) is True
        assert finished.wait(timeout=10)

    assert cache.version_for(_WORKSPACE_ID) == content_version(_PNG)


def test_refresher_logs_a_cache_that_cannot_be_written_and_leaves_the_group_usable(tmp_path: Path) -> None:
    # A file where the cache directory should be: every write under it fails.
    (tmp_path / "wallpapers").write_text("not a directory")
    cache = WallpaperCache(cache_dir=tmp_path / "wallpapers")
    with ConcurrencyGroup(name=f"wallpaper-refresh-{uuid4().hex}") as cg:
        refresher = WallpaperRefresher(
            cache=cache,
            mngr_forward_port=8421,
            make_probe_client=lambda: _shell(wallpaper={"kind": "bundled", "name": "arcs"}),
            concurrency_group=cg,
        )
        assert refresher.request_refresh(_WORKSPACE_ID) is True
        wait_for(lambda: not any(t.name == f"wallpaper-refresh-{_WORKSPACE_ID}" for t in threading.enumerate()))
        # The failed fetch is a logged strand, not a failed one: the group still starts threads.
        landed = threading.Event()
        cg.start_new_thread(target=landed.set, name="after-the-fetch")
        assert landed.wait(timeout=10)

    assert cache.version_for(_WORKSPACE_ID) == ""
    # A group that has shut down starts nothing; the refusal is answered, not raised.
    assert refresher.request_refresh(_WORKSPACE_ID) is False
