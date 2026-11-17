"""The wallpapers the home page paints owned workspace cards with (specs/home-screen-redesign, 4.12).

A workspace keeps its wallpaper inside itself: its shell lists each desktop's
wallpaper on ``GET /api/inventory`` and serves the image at
``GET /wallpapers/<kind>/<name>``. The desktop reads both through the forward
plugin, the way its readiness probes already reach a workspace's shell, when
the workspace is loaded, and keeps the image under the data dir so a card is
painted at once on the next launch. A workspace whose first desktop wears its
theme's default (a null wallpaper) is recorded as having none, so its card is
the plain accent tile and nothing is fetched for it until its next load.
"""

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from urllib.parse import quote

import httpx2
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroupError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.errors import WallpaperFetchError
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.file_utils import atomic_write_bytes
from imbue.mngr.utils.thread_cleanup import start_mngr_thread

CACHE_DIRNAME: Final[str] = "wallpapers"
_VERSION_DIGEST_LENGTH: Final[int] = 12
# What the shell may answer for a wallpaper; anything else is refused unread.
ACCEPTED_WALLPAPER_CONTENT_TYPES: Final[frozenset[str]] = frozenset({"image/png", "image/jpeg", "image/webp"})
MAX_WALLPAPER_BYTES: Final[int] = 8 * 1024 * 1024
WALLPAPER_FETCH_TIMEOUT_SECONDS: Final[float] = 20.0
_INVENTORY_PATH: Final[str] = "/api/inventory"
_WALLPAPER_PATH_PREFIX: Final[str] = "/wallpapers"


@pure
def content_version(content: bytes) -> str:
    """The short digest a cache-busting query carries for ``content``."""
    return hashlib.sha256(content).hexdigest()[:_VERSION_DIGEST_LENGTH]


class WallpaperRecord(FrozenModel):
    """What the cache knows of one workspace's wallpaper: which one, and the version of the stored image."""

    kind: str | None = Field(description="The shell's wallpaper kind (bundled / file); None for no wallpaper")
    name: str | None = Field(description="The wallpaper's name under its kind; None for no wallpaper")
    content_type: str = Field(default="", description="The stored image's media type; empty for no wallpaper")
    version: str = Field(default="", description="The short digest of the stored image; empty for no wallpaper")
    fetched_at: datetime = Field(description="When the record was written (UTC)")


class FetchedWallpaper(FrozenModel):
    """One wallpaper as read from a workspace's shell."""

    kind: str = Field(description="The shell's wallpaper kind")
    name: str = Field(description="The wallpaper's name under its kind")
    content_type: str = Field(description="The image's media type, one of ACCEPTED_WALLPAPER_CONTENT_TYPES")
    body: bytes = Field(description="The image bytes")


class CachedWallpaperImage(FrozenModel):
    """A stored wallpaper image, as the serving route answers it."""

    content_type: str = Field(description="The image's media type")
    version: str = Field(description="The short digest of the bytes")
    body: bytes = Field(description="The image bytes")


@pure
def parse_inventory_wallpaper(inventory: object) -> tuple[str, str] | None:
    """The first desktop's wallpaper from an inventory document, or None for no wallpaper.

    Raises WallpaperFetchError when the document is not shaped like one.
    """
    if not isinstance(inventory, dict):
        raise WallpaperFetchError("The inventory document is not an object")
    desktops = inventory.get("desktops")
    if not isinstance(desktops, list) or not desktops:
        return None
    first = desktops[0]
    if not isinstance(first, dict):
        raise WallpaperFetchError("The inventory's first desktop is not an object")
    wallpaper = first.get("wallpaper")
    if wallpaper is None:
        return None
    if not isinstance(wallpaper, dict):
        raise WallpaperFetchError("The desktop's wallpaper is not an object")
    kind = wallpaper.get("kind")
    name = wallpaper.get("name")
    if not isinstance(kind, str) or not isinstance(name, str) or not kind or not name:
        raise WallpaperFetchError("The desktop's wallpaper names no kind and name")
    return kind, name


def fetch_workspace_wallpaper(
    probe_client: httpx2.Client, mngr_forward_port: int, workspace_id: str
) -> FetchedWallpaper | None:
    """Read the workspace's first desktop's wallpaper through the forward; None when it wears the theme default.

    Raises WallpaperFetchError when the shell did not answer, answered
    something other than an image of an accepted type, or answered too much.
    """
    origin = f"https://127.0.0.1:{mngr_forward_port}"
    headers = {"Host": f"{workspace_id}.localhost"}
    try:
        inventory_response = probe_client.get(f"{origin}{_INVENTORY_PATH}", headers=headers)
    except httpx2.HTTPError as exc:
        raise WallpaperFetchError(f"The inventory of {workspace_id} could not be read: {exc}") from exc
    if inventory_response.status_code != 200:
        raise WallpaperFetchError(f"The inventory of {workspace_id} answered HTTP {inventory_response.status_code}")
    try:
        inventory = inventory_response.json()
    except ValueError as exc:
        raise WallpaperFetchError(f"The inventory of {workspace_id} is not JSON") from exc
    named = parse_inventory_wallpaper(inventory)
    if named is None:
        return None
    kind, name = named
    # Both segments are the shell's own words: encoded, so a name cannot reach past its path.
    image_url = f"{origin}{_WALLPAPER_PATH_PREFIX}/{quote(kind, safe='')}/{quote(name, safe='')}"
    try:
        with probe_client.stream("GET", image_url, headers=headers) as image_response:
            if image_response.status_code != 200:
                raise WallpaperFetchError(
                    f"The wallpaper of {workspace_id} answered HTTP {image_response.status_code}"
                )
            content_type = image_response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in ACCEPTED_WALLPAPER_CONTENT_TYPES:
                raise WallpaperFetchError(
                    f"The wallpaper of {workspace_id} is a {content_type or 'typeless'} response"
                )
            body = _read_body_within_limit(image_response, workspace_id)
    except httpx2.HTTPError as exc:
        raise WallpaperFetchError(f"The wallpaper of {workspace_id} could not be read: {exc}") from exc
    return FetchedWallpaper(kind=kind, name=name, content_type=content_type, body=body)


def _read_body_within_limit(image_response: httpx2.Response, workspace_id: str) -> bytes:
    """The streamed body, read no further than the limit: an oversized answer is refused without buffering it."""
    declared_length = image_response.headers.get("content-length", "")
    if declared_length.isdigit() and int(declared_length) > MAX_WALLPAPER_BYTES:
        raise WallpaperFetchError(f"The wallpaper of {workspace_id} is {declared_length} bytes, over the limit")
    chunks: list[bytes] = []
    read_length = 0
    for chunk in image_response.iter_bytes():
        read_length += len(chunk)
        if read_length > MAX_WALLPAPER_BYTES:
            raise WallpaperFetchError(f"The wallpaper of {workspace_id} is over {MAX_WALLPAPER_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


class WallpaperCache(MutableModel):
    """The on-disk wallpaper cache: one record and at most one image per workspace, read once, on first use."""

    cache_dir: Path = Field(frozen=True, description="Where the records and images live")
    on_change: Callable[[], None] | None = Field(
        default=None,
        description="Invoked after every write, so the channel publisher re-derives the workspace list",
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _record_by_agent_id: dict[str, WallpaperRecord] = PrivateAttr(default_factory=dict)
    _is_loaded: bool = PrivateAttr(default=False)

    def _record_path(self, agent_id: str) -> Path:
        return self.cache_dir / f"{agent_id}.json"

    def _image_path(self, agent_id: str) -> Path:
        return self.cache_dir / f"{agent_id}.img"

    def _load_unlocked(self) -> None:
        if self._is_loaded:
            return
        self._is_loaded = True
        if not self.cache_dir.is_dir():
            return
        for path in self.cache_dir.glob("*.json"):
            try:
                record = WallpaperRecord.model_validate_json(path.read_text())
            except (OSError, ValidationError) as exc:
                logger.warning("Skipped an unreadable wallpaper record {}: {}", path, exc)
                continue
            self._record_by_agent_id[path.stem] = record

    def version_for(self, agent_id: str) -> str:
        with self._lock:
            self._load_unlocked()
            record = self._record_by_agent_id.get(agent_id)
            return record.version if record is not None else ""

    def read_image(self, agent_id: str) -> CachedWallpaperImage | None:
        with self._lock:
            self._load_unlocked()
            record = self._record_by_agent_id.get(agent_id)
            if record is None or not record.version:
                return None
            try:
                body = self._image_path(agent_id).read_bytes()
            except OSError as exc:
                logger.warning("Could not read the cached wallpaper of {}: {}", agent_id, exc)
                return None
            return CachedWallpaperImage(content_type=record.content_type, version=record.version, body=body)

    def store(self, agent_id: str, fetched: FetchedWallpaper | None) -> None:
        """Record what the workspace wears now: the image, or that it wears none."""
        now = datetime.now(timezone.utc)
        if fetched is None:
            record = WallpaperRecord(kind=None, name=None, fetched_at=now)
        else:
            record = WallpaperRecord(
                kind=fetched.kind,
                name=fetched.name,
                content_type=fetched.content_type,
                version=content_version(fetched.body),
                fetched_at=now,
            )
        with self._lock:
            self._load_unlocked()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            image_path = self._image_path(agent_id)
            if fetched is None:
                image_path.unlink(missing_ok=True)
            else:
                atomic_write_bytes(image_path, fetched.body)
            atomic_write(self._record_path(agent_id), json.dumps(record.model_dump(mode="json")))
            self._record_by_agent_id[agent_id] = record
        if self.on_change is not None:
            self.on_change()


class WallpaperRefresher(MutableModel):
    """Runs one wallpaper fetch per workspace at a time on a background thread, writing the cache when it lands."""

    cache: WallpaperCache = Field(frozen=True, description="Where a fetched wallpaper is written")
    mngr_forward_port: int = Field(frozen=True, description="The forward plugin's port")
    make_probe_client: Callable[[], httpx2.Client] = Field(
        frozen=True, description="Builds a client that reaches the forward as the owner (the readiness probes' own)"
    )
    concurrency_group: ConcurrencyGroup = Field(frozen=True, description="Where the fetch threads run")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _in_flight: set[str] = PrivateAttr(default_factory=set)

    def request_refresh(self, workspace_id: str) -> bool:
        """Start a fetch for the workspace unless one is already running; whether one was started."""
        with self._lock:
            if workspace_id in self._in_flight:
                return False
            self._in_flight.add(workspace_id)
        try:
            start_mngr_thread(
                self.concurrency_group,
                target=self._fetch_and_store,
                args=(workspace_id,),
                name=f"wallpaper-refresh-{workspace_id}",
                daemon=True,
                # A fetch that fails is logged, not raised into the app's root group.
                is_checked=False,
                on_failure=_log_wallpaper_refresh_failure,
            )
        except (OSError, RuntimeError, ConcurrencyGroupError) as exc:
            with self._lock:
                self._in_flight.discard(workspace_id)
            logger.warning("Could not start the wallpaper fetch of {}: {}", workspace_id, exc)
            return False
        return True

    def _fetch_and_store(self, workspace_id: str) -> None:
        try:
            with self.make_probe_client() as probe_client:
                fetched = fetch_workspace_wallpaper(probe_client, self.mngr_forward_port, workspace_id)
        except (WallpaperFetchError, OSError) as exc:
            # OSError: the forward's CA certificate could not be read, so no client could be built.
            logger.debug("Kept the cached wallpaper of {}: {}", workspace_id, exc)
            return
        finally:
            with self._lock:
                self._in_flight.discard(workspace_id)
        try:
            self.cache.store(workspace_id, fetched)
        except OSError as exc:
            logger.warning("Could not write the wallpaper of {} to the cache: {}", workspace_id, exc)
            return
        logger.debug("Stored the wallpaper of {} ({})", workspace_id, "none" if fetched is None else fetched.name)


def _log_wallpaper_refresh_failure(exc: BaseException) -> None:
    logger.opt(exception=exc).warning("A wallpaper fetch failed")
