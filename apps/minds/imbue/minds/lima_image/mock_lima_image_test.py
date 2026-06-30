"""Concrete in-memory mock implementations of the lima_image interfaces for unit tests.

These let ``ensure_test.py`` exercise the full ensure-image orchestration (seeding,
verification, retention, progress, fallbacks) without the real desync / minisign /
qemu-img binaries -- the binaries are exercised separately by the binary-gated
``test_lima_image_e2e.py`` integration test.
"""

import shutil
from pathlib import Path

from pydantic import Field

from imbue.minds.errors import LimaImageDownloadError
from imbue.minds.errors import LimaImageVerificationError
from imbue.minds.lima_image.data_types import LimaImagePrefetchState
from imbue.minds.lima_image.interfaces import ImageChunkStoreInterface
from imbue.minds.lima_image.interfaces import ImageFormatConverterInterface
from imbue.minds.lima_image.interfaces import LimaImageProgressSinkInterface
from imbue.minds.lima_image.interfaces import ManifestFetcherInterface
from imbue.minds.lima_image.interfaces import ProcessOutputCallback
from imbue.minds.lima_image.interfaces import SignatureVerifierInterface


class InMemoryManifestFetcher(ManifestFetcherInterface):
    """Serves manifest/index/signature objects from an in-memory url->bytes map."""

    objects_by_url: dict[str, bytes] = Field(default_factory=dict, description="Published objects keyed by URL")

    def fetch_optional_bytes(self, url: str) -> bytes | None:
        return self.objects_by_url.get(url)

    def download_to_file(self, url: str, destination: Path) -> None:
        body = self.objects_by_url.get(url)
        if body is None:
            raise LimaImageDownloadError(f"No object at {url}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)


class AcceptingSignatureVerifier(SignatureVerifierInterface):
    """A verifier that accepts any signature (used for the happy path)."""

    def verify_detached(self, *, signed_file: Path, signature_file: Path, public_key: str) -> None:
        return None


class RejectingSignatureVerifier(SignatureVerifierInterface):
    """A verifier that rejects every signature (simulates tamper / wrong key)."""

    def verify_detached(self, *, signed_file: Path, signature_file: Path, public_key: str) -> None:
        raise LimaImageVerificationError("mock signature rejection")


class FixedRawChunkStore(ImageChunkStoreInterface):
    """Writes pre-seeded raw bytes (keyed by index object name) as the 'assembled' image."""

    raw_bytes_by_index_name: dict[str, bytes] = Field(
        default_factory=dict, description="Assembled raw bytes keyed by the index file's name"
    )
    seed_index_names_seen: list[str] = Field(
        default_factory=list, description="Index names for which a seed blob was supplied (asserts seeding fired)"
    )

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
        if seed_index_file is not None and seed_blob_file is not None:
            self.seed_index_names_seen.append(index_file.name)
        body = self.raw_bytes_by_index_name.get(index_file.name)
        if body is None:
            raise LimaImageDownloadError(f"No assembled bytes configured for index {index_file.name}")
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_bytes(body)
        if on_output is not None:
            on_output("100% assembled", False)


class CopyingImageFormatConverter(ImageFormatConverterInterface):
    """Stands in for qemu-img by copying bytes through unchanged (preserves identity for seeding tests)."""

    def convert_raw_to_qcow2(self, *, raw_file: Path, qcow2_file: Path) -> None:
        qcow2_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(raw_file, qcow2_file)

    def convert_qcow2_to_raw(self, *, qcow2_file: Path, raw_file: Path) -> None:
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(qcow2_file, raw_file)


class RecordingProgressSink(LimaImageProgressSinkInterface):
    """Records every progress state in order for assertions, and persists the latest for reads."""

    states: list[LimaImagePrefetchState] = Field(default_factory=list, description="All states in write order")

    def write_state(self, state: LimaImagePrefetchState) -> None:
        self.states.append(state)

    def read_state(self) -> LimaImagePrefetchState | None:
        return self.states[-1] if self.states else None
