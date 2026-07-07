from pathlib import Path
from typing import Final

import httpx
from loguru import logger
from pydantic import Field

from imbue.minds.errors import LimaImageDownloadError
from imbue.minds.lima_image.interfaces import ManifestFetcherInterface

# Small-object fetch (manifest, signature, index): generous but bounded.
MANIFEST_FETCH_TIMEOUT_SECONDS: Final[float] = 60.0

# Streaming download chunk size for index files.
_DOWNLOAD_CHUNK_BYTES: Final[int] = 1024 * 256


class HttpxManifestFetcher(ManifestFetcherInterface):
    """Fetches manifest/index/signature objects over HTTP(S) using httpx."""

    timeout_seconds: float = Field(
        default=MANIFEST_FETCH_TIMEOUT_SECONDS, frozen=True, description="Per-request timeout"
    )

    def fetch_optional_bytes(self, url: str) -> bytes | None:
        try:
            response = httpx.get(url, timeout=self.timeout_seconds, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise LimaImageDownloadError(f"Failed to fetch {url}: {exc}") from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            logger.debug("No object at {} (404)", url)
            return None
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LimaImageDownloadError(f"Unexpected status {response.status_code} fetching {url}") from exc
        return response.content

    def download_to_file(self, url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with httpx.stream("GET", url, timeout=self.timeout_seconds, follow_redirects=True) as response:
                response.raise_for_status()
                with destination.open("wb") as out:
                    for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_BYTES):
                        out.write(chunk)
        except httpx.HTTPError as exc:
            raise LimaImageDownloadError(f"Failed to download {url}: {exc}") from exc
