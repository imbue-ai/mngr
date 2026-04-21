"""Helpers for Lima's image-download cache.

Lima stores downloaded VM images under a deterministic, content-addressable
path keyed on the URL. If a file is already there with the expected SHA256,
`limactl start` skips the download. Electron's prefetch writes into this
path; Python callers can wait on the prefetch before invoking `mngr create`.

Cache layout (stable across Lima versions at time of writing):
    <cache_root>/download/by-url-sha256/<hex(sha256(url))>/data

    macOS:  ~/Library/Caches/lima
    Linux:  $XDG_CACHE_HOME/lima  (fallback: ~/.cache/lima)
"""

import hashlib
import os
import sys
from pathlib import Path

from imbue.mngr.utils.polling import poll_until


def get_lima_cache_data_path(image_url: str) -> Path:
    """Return the path where Lima caches the downloaded image for this URL.

    The path is content-addressable on the URL, matching Lima's internal
    scheme. The returned file may or may not exist yet.
    """
    url_hash = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
    if sys.platform == "darwin":
        cache_root = Path.home() / "Library" / "Caches" / "lima"
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        cache_root = Path(xdg_cache) / "lima" if xdg_cache else Path.home() / ".cache" / "lima"
    return cache_root / "download" / "by-url-sha256" / url_hash / "data"


def compute_file_sha256(path: Path) -> str:
    """Compute the hex SHA256 of a file's contents."""
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def is_image_cached(image_url: str, expected_sha256: str | None) -> bool:
    """Return True if the cached image exists and (when supplied) matches the expected digest.

    When expected_sha256 is None the existence of the file is enough. This
    matches Lima's behavior when the yaml has no `digest:` set.
    """
    cache_path = get_lima_cache_data_path(image_url)
    if not cache_path.is_file():
        return False
    if expected_sha256 is None:
        return True
    return compute_file_sha256(cache_path) == expected_sha256


def wait_for_image_ready(
    image_url: str,
    expected_sha256: str | None,
    timeout_seconds: float,
    poll_interval_seconds: float = 1.0,
) -> bool:
    """Poll the Lima cache until the image is ready or timeout expires.

    Returns True if the image is cached and (when supplied) digest-verified
    before timeout, False otherwise. Never raises on missing file / timeout
    so callers can fall through to letting Lima do the download itself.
    """
    return poll_until(
        condition=lambda: is_image_cached(image_url, expected_sha256),
        timeout=timeout_seconds,
        poll_interval=poll_interval_seconds,
    )
