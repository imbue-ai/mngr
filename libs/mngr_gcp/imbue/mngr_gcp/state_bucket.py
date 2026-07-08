from collections.abc import Iterator
from contextlib import AbstractContextManager
from contextlib import contextmanager
from typing import Any
from typing import Final

from google.api_core import exceptions as google_api_exceptions
from google.auth.credentials import Credentials
from google.cloud import storage
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr_vps import state_keys
from imbue.mngr_vps.state_bucket_base import BaseObjectStoreVolume
from imbue.mngr_vps.state_bucket_base import BaseStateBucket
from imbue.mngr_vps.state_bucket_base import ObjectStoreEntry

# GCS uses ``Standard`` as the default storage class. The state bucket holds small
# control-plane records + a captured host_dir mirror, so the default class is the
# right tradeoff (low latency, no minimum retention). Encryption at rest is on by
# default for every GCS bucket -- no SSE configuration needed.
_GCS_DEFAULT_STORAGE_CLASS: Final[str] = "STANDARD"


class GcsStateBucketError(MngrError):
    """A GCS state-bucket operation failed."""


class GcsStateBucket(BaseStateBucket):
    """Reads/writes mngr control-plane state in a GCS bucket, readable while offline.

    The GCP analog of ``S3StateBucket`` / ``BlobStateBucket``: a GCS bucket holds
    the offline ``host_dir`` mirror keyed by host id, written by the mngr host
    machine with the operator's credentials so a TERMINATED instance's captured
    ``host_dir`` is readable without SSH and without the GCE metadata size limit.
    The host + agent records still live in GCE instance metadata (see
    ``_GceMetadataHostStateStore`` in ``backend.py``); the bucket primarily backs
    the offline ``host_dir`` capture, but it satisfies the full ``BaseStateBucket``
    interface so the shared ``BucketHostDirBackend`` works against it unchanged.

    The cloud-agnostic record marshalling + key layout live on ``BaseStateBucket``;
    this class supplies the GCS client, the raw object primitives, error
    translation, and the bucket lifecycle.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    credentials: Credentials = Field(frozen=True, description="Google ADC credentials")
    project_id: str = Field(frozen=True, description="GCP project the bucket lives in")
    region: str = Field(frozen=True, description="GCS region the bucket lives in (e.g. 'us-west1')")
    bucket_name: str = Field(frozen=True, description="Name of the GCS bucket holding mngr state")
    _cached_storage_client: Any = PrivateAttr(default=None)

    def _client(self) -> Any:
        """Return the GCS client, building and caching it from the credentials on first use."""
        if self._cached_storage_client is None:
            self._cached_storage_client = storage.Client(project=self.project_id, credentials=self.credentials)
        return self._cached_storage_client

    def _bucket(self) -> Any:
        """Return a bare bucket handle (no existence probe)."""
        return self._client().bucket(self.bucket_name)

    @property
    def _store_label(self) -> str:
        return f"GCS bucket {self.bucket_name}"

    def _translate_errors(self) -> AbstractContextManager[None]:
        return _translate_gcs_errors(self.bucket_name)

    def _is_not_found(self, error: MngrError) -> bool:
        return _is_gcs_not_found(error)

    @property
    def _bucket_error_type(self) -> type[MngrError]:
        return GcsStateBucketError

    def _make_host_dir_volume(self) -> Volume:
        return GcsVolume(
            credentials=self.credentials,
            project_id=self.project_id,
            bucket_name=self.bucket_name,
        )

    def _prefix_has_any_object(self, prefix: str) -> bool:
        # Cap at one to keep the probe cheap (no full listing).
        for _ in self._client().list_blobs(self.bucket_name, prefix=prefix, max_results=1):
            return True
        return False

    def bucket_exists(self) -> bool:
        """Return whether the bucket already exists (read-only lookup)."""
        try:
            return self._client().lookup_bucket(self.bucket_name) is not None
        except google_api_exceptions.GoogleAPICallError as e:
            raise GcsStateBucketError(f"Failed to check existence of GCS bucket {self.bucket_name!r}: {e}") from e

    def ensure_bucket(self) -> bool:
        """Idempotently create the state bucket, returning True iff it was created.

        Read-only-first: a lookup precedes any create, so a re-run on an
        already-prepared bucket issues no write. The created bucket is private (no
        public access), encrypted at rest by default (GCS bucket encryption is
        always on), uniform-access (no per-object ACLs), and labeled
        ``managed-by=mngr``.
        """
        if self.bucket_exists():
            logger.debug("GCS state bucket {} already exists; skipping create", self.bucket_name)
            return False
        bucket = self._bucket()
        bucket.storage_class = _GCS_DEFAULT_STORAGE_CLASS
        # Uniform bucket-level access: IAM-only, no per-object ACLs (so the
        # operator's principal grant flows through cleanly).
        bucket.iam_configuration.uniform_bucket_level_access_enabled = True
        bucket.labels = {state_keys.MANAGED_BY_TAG_KEY: state_keys.MANAGED_BY_TAG_VALUE}
        with _translate_gcs_errors(self.bucket_name):
            self._client().create_bucket(bucket, location=self.region)
        logger.info("Created GCS state bucket {} in region {}", self.bucket_name, self.region)
        return True

    def delete_bucket(self) -> None:
        """Empty and delete the bucket. Idempotent (no error if already absent)."""
        if not self.bucket_exists():
            return
        with _translate_gcs_errors(self.bucket_name):
            # ``force=True`` deletes all objects first, then the bucket itself, in
            # one call. Matches the AWS path that empties then deletes.
            self._client().get_bucket(self.bucket_name).delete(force=True)
        logger.info("Deleted GCS state bucket {} in region {}", self.bucket_name, self.region)

    def _put_object(self, key: str, body: str) -> None:
        with _translate_gcs_errors(self.bucket_name):
            self._bucket().blob(key).upload_from_string(body.encode("utf-8"))

    def _read_object_bytes(self, key: str) -> bytes:
        return self._bucket().blob(key).download_as_bytes()

    def _delete_single_object(self, key: str) -> None:
        self._bucket().blob(key).delete()

    def _list_keys(self, prefix: str) -> list[str]:
        with _translate_gcs_errors(self.bucket_name):
            return [blob.name for blob in self._client().list_blobs(self.bucket_name, prefix=prefix)]

    def _delete_keys(self, keys: list[str]) -> None:
        # GCS has no batch-delete primitive; loop one at a time (each idempotent via
        # ``_delete_object``). Matches the Azure pattern.
        for key in keys:
            self._delete_object(key)


class GcsVolume(BaseObjectStoreVolume):
    """A ``Volume`` backed by a GCS bucket, for reading a host's offline host_dir.

    Maps volume-relative paths to GCS object names under whatever prefix it is
    ``scoped()`` to. Reads use the operator's credentials (the same ADC as
    ``GcsStateBucket``). The shared object-store logic (listing / existence / read
    / write / delete) lives on ``BaseObjectStoreVolume``; this class supplies only
    the GCS client + SDK primitives + error seam. Mirrors ``S3Volume`` and
    ``BlobVolume``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    credentials: Credentials = Field(frozen=True, description="Google ADC credentials")
    project_id: str = Field(frozen=True, description="GCP project the bucket lives in")
    bucket_name: str = Field(frozen=True, description="Name of the GCS bucket")
    _cached_storage_client: Any = PrivateAttr(default=None)

    def _client(self) -> Any:
        if self._cached_storage_client is None:
            self._cached_storage_client = storage.Client(project=self.project_id, credentials=self.credentials)
        return self._cached_storage_client

    def _bucket(self) -> Any:
        return self._client().bucket(self.bucket_name)

    def _translate_errors(self) -> AbstractContextManager[None]:
        return _translate_gcs_errors(self.bucket_name)

    def _is_not_found(self, error: MngrError) -> bool:
        return _is_gcs_not_found(error)

    @property
    def _bucket_error_type(self) -> type[MngrError]:
        return GcsStateBucketError

    def _make_missing_file_error(self, path: str) -> MngrError:
        return GcsStateBucketError(f"File {path!r} does not exist in bucket {self.bucket_name!r}")

    def _iter_delimited_entries(self, prefix: str) -> Iterator[ObjectStoreEntry]:
        # A delimited list yields blobs + a ``prefixes`` attribute on the
        # iterator with the immediate sub-"directories" (each as a string ending
        # in "/"). The iterator must be consumed before ``prefixes`` is read.
        iterator = self._client().list_blobs(self.bucket_name, prefix=prefix, delimiter="/")
        # Files directly under the prefix.
        for blob in iterator:
            updated = blob.updated
            yield ObjectStoreEntry(
                name=blob.name,
                is_directory=False,
                mtime=int(updated.timestamp()) if updated is not None else 0,
                size=blob.size or 0,
            )
        for sub_prefix in iterator.prefixes:
            yield ObjectStoreEntry(name=sub_prefix.rstrip("/"), is_directory=True, mtime=0, size=0)

    def _prefix_has_any_object(self, prefix: str) -> bool:
        for _ in self._client().list_blobs(self.bucket_name, prefix=prefix, max_results=1):
            return True
        return False

    def _has_object_at_key(self, key: str) -> bool:
        return self._bucket().blob(key).exists()

    def _read_object_bytes(self, key: str) -> bytes:
        return self._bucket().blob(key).download_as_bytes()

    def _delete_single_object(self, key: str) -> None:
        self._bucket().blob(key).delete()

    def _delete_prefix(self, prefix: str) -> None:
        for blob in list(self._client().list_blobs(self.bucket_name, prefix=prefix)):
            blob.delete()

    def _write_object(self, key: str, content: bytes) -> None:
        self._bucket().blob(key).upload_from_string(content)


def _is_gcs_not_found(error: MngrError) -> bool:
    """Return whether a translated ``GcsStateBucketError`` wraps a GCS not-found."""
    return isinstance(error.__cause__, google_api_exceptions.NotFound)


@contextmanager
def _translate_gcs_errors(bucket_name: str) -> Iterator[None]:
    """Translate ``google.api_core`` errors into ``GcsStateBucketError`` within the block."""
    try:
        yield
    except google_api_exceptions.GoogleAPICallError as e:
        raise GcsStateBucketError(f"GCS operation on bucket {bucket_name!r} failed: {e}") from e
