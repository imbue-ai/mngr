"""Keep transcript SSE frames small by serving large images by reference.

A tool result or pasted message can carry multi-megabyte base64 images. Inlining
those in the SSE ``event`` frame would bloat every EventSource message, so before
emitting an event we ``externalize`` its large images: decode + stash the bytes in
a bounded in-memory cache keyed by the parser's stable image id, and strip the
base64 from the event (leaving just ``id`` + ``media_type``). The chat page then
fetches those by id from ``GET /api/agents/<name>/timage/<id>``. Small images stay
inline (no extra round-trip).
"""

from __future__ import annotations

import base64
import threading
import time
from typing import Any

# Below this base64 length an image is cheap enough to inline in the SSE event;
# at or above it we externalize and serve by reference.
_INLINE_MAX_CHARS = 200 * 1024
# Bounded cache so a long session of screenshots cannot grow memory without limit.
_CACHE_TTL_SECONDS = 1800.0
_CACHE_MAX_ENTRIES = 128
_CACHE_MAX_BYTES = 128 * 1024 * 1024

_CACHE: dict[str, tuple[float, str, bytes]] = {}
_LOCK = threading.Lock()


def _evict_locked() -> None:
    while _CACHE and (
        len(_CACHE) > _CACHE_MAX_ENTRIES or sum(len(v[2]) for v in _CACHE.values()) > _CACHE_MAX_BYTES
    ):
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        del _CACHE[oldest]


def externalize_event_images(event: dict[str, Any]) -> None:
    """Strip large base64 images out of an event, caching their bytes by id.

    Mutates ``event`` in place: any image whose base64 exceeds the inline cap has
    its ``data`` removed (the client fetches it by ``id``); smaller images are left
    inline. A base64 payload that fails to decode is left as-is.
    """
    images = event.get("images")
    if not isinstance(images, list):
        return
    for image in images:
        data = image.get("data")
        image_id = image.get("id")
        if not isinstance(data, str) or not image_id or len(data) < _INLINE_MAX_CHARS:
            continue
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            continue
        media_type = image.get("media_type") or "image/png"
        with _LOCK:
            _CACHE[image_id] = (time.monotonic(), media_type, raw)
            _evict_locked()
        image.pop("data", None)


def get_cached_image(image_id: str) -> tuple[str, bytes] | None:
    """Return ``(media_type, bytes)`` for a previously externalized image, or None."""
    with _LOCK:
        entry = _CACHE.get(image_id)
        if entry is None:
            return None
        stored_at, media_type, raw = entry
        if time.monotonic() - stored_at >= _CACHE_TTL_SECONDS:
            _CACHE.pop(image_id, None)
            return None
        return media_type, raw
