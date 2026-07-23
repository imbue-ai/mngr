from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from pathlib import Path

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.lima_image.data_types import LimaImagePrefetchState

# Called with (line, is_stdout) for each line a subprocess emits, mirroring
# ConcurrencyGroup's on_output contract so progress can be surfaced live.
ProcessOutputCallback = Callable[[str, bool], None]


class ManifestFetcherInterface(MutableModel, ABC):
    """Fetches small manifest/index/signature objects from the chunk-store origin."""

    @abstractmethod
    def fetch_optional_bytes(self, url: str) -> bytes | None:
        """Return the body at ``url``, or None if it does not exist (HTTP 404).

        Raises ``LimaImageDownloadError`` for any other failure (network error,
        5xx, etc.) so a transient outage is never mistaken for "no such version".
        """

    @abstractmethod
    def download_to_file(self, url: str, destination: Path) -> None:
        """Download ``url`` to ``destination``. Raises ``LimaImageDownloadError`` on any failure."""


class SignatureVerifierInterface(MutableModel, ABC):
    """Verifies a detached signature over a downloaded file against a trusted public key."""

    @abstractmethod
    def verify_detached(self, *, signed_file: Path, signature_file: Path, public_key: str) -> None:
        """Raise ``LimaImageVerificationError`` unless ``signature_file`` is a valid signature of ``signed_file``."""


class ImageChunkStoreInterface(MutableModel, ABC):
    """Assembles a raw image from a content-addressed chunk store, optionally seeded by a prior image."""

    @abstractmethod
    def extract_image(
        self,
        *,
        index_file: Path,
        chunk_store_url: str,
        output_file: Path,
        local_cache_dir: Path,
        seed_index_file: Path | None,
        seed_blob_file: Path | None,
        on_output: ProcessOutputCallback | None,
    ) -> None:
        """Reassemble the raw image described by ``index_file`` into ``output_file``.

        Resumable in place (a partially-extracted ``output_file`` is reused). When
        ``seed_index_file`` / ``seed_blob_file`` are given, chunks already present
        in the seed are copied locally instead of downloaded. Raises
        ``LimaImageDownloadError`` on failure.
        """


class LimaImageProgressSinkInterface(MutableModel, ABC):
    """Persists the ensure-image operation's progress for the create gate to read."""

    @abstractmethod
    def write_state(self, state: LimaImagePrefetchState) -> None:
        """Persist ``state`` so a concurrent reader (the create gate) observes the latest progress."""

    @abstractmethod
    def read_state(self) -> LimaImagePrefetchState | None:
        """Return the last-written state, or None if none has been written yet."""
