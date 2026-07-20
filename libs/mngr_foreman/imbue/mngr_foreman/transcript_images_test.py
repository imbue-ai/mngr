"""Tests for by-reference externalization of large transcript images."""

from __future__ import annotations

import base64

import pytest

from imbue.mngr_foreman import transcript_images
from imbue.mngr_foreman.transcript_images import externalize_event_images
from imbue.mngr_foreman.transcript_images import get_cached_image


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    transcript_images._CACHE.clear()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def test_small_image_stays_inline() -> None:
    event = {"type": "tool_result", "images": [{"id": "x-0", "media_type": "image/png", "data": _b64(b"tiny")}]}
    externalize_event_images(event)
    # Small: left inline, nothing cached.
    assert "data" in event["images"][0]
    assert get_cached_image("x-0") is None


def test_large_image_externalized_and_served() -> None:
    raw = b"\x89PNG" + b"z" * (transcript_images._INLINE_MAX_CHARS)  # base64 will exceed the inline cap
    event = {"type": "tool_result", "images": [{"id": "big-0", "media_type": "image/png", "data": _b64(raw)}]}
    externalize_event_images(event)
    # Large: base64 stripped from the event, bytes served by id.
    assert "data" not in event["images"][0]
    assert event["images"][0]["id"] == "big-0"
    cached = get_cached_image("big-0")
    assert cached is not None
    assert cached[0] == "image/png"
    assert cached[1] == raw


def test_undecodable_large_data_left_alone() -> None:
    event = {"type": "tool_result", "images": [{"id": "bad-0", "media_type": "image/png", "data": "!!!" * 200000}]}
    externalize_event_images(event)
    assert "data" in event["images"][0]  # couldn't decode -> not externalized
    assert get_cached_image("bad-0") is None


def test_event_without_images_is_noop() -> None:
    event = {"type": "assistant_message", "text": "hi"}
    externalize_event_images(event)  # must not raise
    assert "images" not in event


def test_missing_id_returns_none() -> None:
    assert get_cached_image("never-cached") is None
