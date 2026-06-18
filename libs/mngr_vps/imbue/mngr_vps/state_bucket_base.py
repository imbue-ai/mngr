import json
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import AbstractContextManager

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.volume import BaseVolume
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import HostId
from imbue.mngr_vps import state_keys


def _as_dir_prefix(path: str) -> str:
    """Normalize a volume path to an object-store directory prefix (no leading slash, trailing slash)."""
    cleaned = path.strip("/")
    return f"{cleaned}/" if cleaned else ""


class ObjectStoreEntry(FrozenModel):
    """A normalized entry from a delimited object-store listing (a file or a sub-"directory").

    Each cloud's ``_iter_delimited_entries`` maps its SDK page shape (S3
    ``CommonPrefixes`` / ``Contents``, Azure ``walk_blobs`` ``BlobPrefix`` /
    ``BlobProperties``) into this shape, so the shared ``listdir`` logic needs no
    per-cloud branching. ``name`` is the full object key (or, for a directory, the
    common prefix, with its trailing slash already stripped); ``mtime`` / ``size``
    are zero for directory entries.
    """

    name: str = Field(description="Full object key (file) or common prefix (directory, trailing slash stripped)")
    is_directory: bool = Field(description="Whether this entry is a synthesized sub-directory")
    mtime: int = Field(description="Last modification time as Unix timestamp (0 for directories)")
    size: int = Field(description="Size in bytes (0 for directories)")


class _ObjectStoreErrorSeam(ABC):
    """Shared error-translation contract for object-store-backed buckets and volumes.

    Both clouds repeat the pattern "run an SDK op, translate any SDK error into
    the cloud's ``MngrError`` subtype, and treat a not-found specially". This seam
    captures the two cloud-specific decisions (how to wrap, how to recognize
    not-found) so the shared bucket/volume logic can be written once.
    """

    @abstractmethod
    def _translate_errors(self) -> AbstractContextManager[None]:
        """Return a context manager that wraps any SDK error raised in the block as the cloud's bucket error."""

    @abstractmethod
    def _is_not_found(self, error: MngrError) -> bool:
        """Return whether a translated bucket error represents a missing object/bucket."""

    @property
    @abstractmethod
    def _bucket_error_type(self) -> type[MngrError]:
        """The cloud's ``MngrError`` subtype that ``_translate_errors`` raises (caught to special-case not-found)."""


class BaseStateBucket(MutableModel, _ObjectStoreErrorSeam, ABC):
    """Cloud-agnostic record marshalling + key layout for a provider state bucket.

    A provider state bucket holds mngr's control-plane state -- the full host
    record and per-agent records, plus the offline ``host_dir`` mirror -- keyed by
    host id under the shared ``state_keys`` layout. The marshalling (JSON
    encode/decode), key layout, agent-listing, and offline-read volume are
    identical across clouds; only the raw object get/put/list/delete and the
    SDK Volume differ. This base implements the former ONCE in terms of a handful
    of abstract primitives each cloud supplies (S3, Azure Blob, ...).

    Concrete subclasses keep only: SDK client construction/caching, the raw
    primitives below, the error seam (``_translate_errors`` / ``_is_not_found`` /
    ``_bucket_error_type``), the cloud ``Volume``, and the bucket/account lifecycle
    (ensure/exists/delete). They structurally satisfy the ``StateBucket`` Protocol
    in ``host_state_store.py`` (which stays a Protocol so ``BucketHostStateStore``
    need not import this concrete base).
    """

    @abstractmethod
    def _put_object(self, key: str, body: str) -> None:
        """Write an object's body (UTF-8), overwriting any existing object."""

    @abstractmethod
    def _read_object_bytes(self, key: str) -> bytes:
        """Return an object's raw bytes, run inside ``_translate_errors`` by the base (raises if absent)."""

    @abstractmethod
    def _delete_single_object(self, key: str) -> None:
        """Delete a single object, run inside ``_translate_errors`` by the base (may raise not-found)."""

    @abstractmethod
    def _list_keys(self, prefix: str) -> list[str]:
        """Return every object key under ``prefix``."""

    @abstractmethod
    def _delete_keys(self, keys: list[str]) -> None:
        """Delete the given keys. Idempotent; a no-op on an empty list."""

    @abstractmethod
    def _prefix_has_any_object(self, prefix: str) -> bool:
        """Return whether any object exists under ``prefix``, run inside ``_translate_errors`` by the base."""

    @abstractmethod
    def _make_host_dir_volume(self) -> Volume:
        """Return the un-scoped cloud ``Volume`` for this bucket (the base scopes it per host)."""

    @property
    @abstractmethod
    def _store_label(self) -> str:
        """Human-readable bucket/container name for log messages."""

    def _get_object(self, key: str) -> str | None:
        """Return an object's body (UTF-8), or None if no object exists at ``key``."""
        try:
            with self._translate_errors():
                return self._read_object_bytes(key).decode("utf-8")
        except self._bucket_error_type as e:
            if self._is_not_found(e):
                return None
            raise

    def _delete_object(self, key: str) -> None:
        """Delete a single object. Idempotent (no error if absent)."""
        try:
            with self._translate_errors():
                self._delete_single_object(key)
        except self._bucket_error_type as e:
            if self._is_not_found(e):
                return
            raise

    def _prefix_has_objects(self, prefix: str) -> bool:
        """Return whether any object exists under ``prefix`` (a cheap probe, capped at one)."""
        with self._translate_errors():
            return self._prefix_has_any_object(prefix)

    def write_host_record_json(self, host_id: HostId, record_json: str) -> None:
        """Write the host record JSON for a host, overwriting any existing object."""
        self._put_object(state_keys.host_state_key(host_id), record_json)

    def read_host_record_json(self, host_id: HostId) -> str | None:
        """Return the host record JSON for a host, or None if no object exists."""
        return self._get_object(state_keys.host_state_key(host_id))

    def write_agent_record(self, host_id: HostId, agent_id: str, data: Mapping[str, object]) -> None:
        """Write a single agent's record (serialized as JSON) under the host's prefix."""
        self._put_object(state_keys.agent_key(host_id, agent_id), json.dumps(dict(data)))

    def list_agent_records(self, host_id: HostId) -> list[dict]:
        """Return every agent record stored under the host's ``agents/`` prefix.

        A stored object that is not valid JSON (externally edited / corrupted) is
        skipped with a warning rather than crashing the listing.
        """
        records: list[dict] = []
        for key in self._list_keys(state_keys.agents_prefix(host_id)):
            body = self._get_object(key)
            if body is None:
                continue
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as e:
                logger.warning("Skipping unparseable agent record {} in {}: {}", key, self._store_label, e)
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                logger.warning("Skipping agent record {} in {}: not a JSON object", key, self._store_label)
        return records

    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None:
        """Delete a single agent's record. Idempotent (no error if absent)."""
        self._delete_object(state_keys.agent_key(host_id, agent_id))

    def delete_host_state(self, host_id: HostId) -> None:
        """Delete every object under the host's prefix. Idempotent."""
        self._delete_keys(self._list_keys(f"{state_keys.host_prefix(host_id)}/"))

    def has_any_host_state(self) -> bool:
        """Return whether any object exists under the ``hosts/`` prefix."""
        return self._prefix_has_objects(f"{state_keys.HOSTS_PREFIX}/")

    def host_dir_prefix_has_objects(self, host_id: HostId) -> bool:
        """Return whether any object exists under the host's ``host_dir/`` prefix.

        Used by the offline-read path as a light existence probe: an empty prefix
        means nothing was captured yet (the host was never ``mngr stop``-ped, or
        idle-self-poweroffed with no operator to capture it).
        """
        return self._prefix_has_objects(state_keys.host_dir_prefix(host_id))

    def volume_for_host(self, host_id: HostId) -> Volume:
        """Return a Volume scoped to ``hosts/<host_id_hex>/host_dir/`` for offline reads.

        Both the capture (at ``mngr stop``) and these reads use the operator's
        credentials, so no instance identity is required. The returned volume is
        rooted at the host's ``host_dir`` tree, matching how
        ``OfflineHostWithVolume`` addresses files (relative to ``host_dir``).
        """
        host_dir_prefix = state_keys.host_dir_prefix(host_id).rstrip("/")
        return self._make_host_dir_volume().scoped(host_dir_prefix)


class BaseObjectStoreVolume(BaseVolume, _ObjectStoreErrorSeam):
    """A ``Volume`` backed by a flat object store (S3 / Azure Blob), for offline host_dir reads.

    Object stores have no real directories, so a "directory" is the set of keys
    sharing a prefix; ``listdir`` synthesizes directory entries from the common
    prefixes a delimited list returns. The shared listing / existence / read /
    write / delete logic lives here once; concrete subclasses (``S3Volume``,
    ``BlobVolume``) supply only the SDK primitives below plus the error seam.
    """

    @abstractmethod
    def _iter_delimited_entries(self, prefix: str) -> Iterator[ObjectStoreEntry]:
        """Yield the immediate children of ``prefix`` (one delimited listing), run inside ``_translate_errors``."""

    @abstractmethod
    def _prefix_has_any_object(self, prefix: str) -> bool:
        """Return whether any object exists under ``prefix``, run inside ``_translate_errors`` by the base."""

    @abstractmethod
    def _has_object_at_key(self, key: str) -> bool:
        """Return whether an object exists at exactly ``key``, run inside ``_translate_errors`` by the base."""

    @abstractmethod
    def _read_object_bytes(self, key: str) -> bytes:
        """Return an object's raw bytes, run inside ``_translate_errors`` by the base (raises if absent)."""

    @abstractmethod
    def _delete_single_object(self, key: str) -> None:
        """Delete a single object, run inside ``_translate_errors`` by the base (may raise not-found)."""

    @abstractmethod
    def _delete_prefix(self, prefix: str) -> None:
        """Delete every object under ``prefix``, run inside ``_translate_errors`` by the base."""

    @abstractmethod
    def _write_object(self, key: str, content: bytes) -> None:
        """Write a single object (overwriting), run inside ``_translate_errors`` by the base."""

    @abstractmethod
    def _make_missing_file_error(self, path: str) -> MngrError:
        """Build the cloud's "file does not exist" error for a missing ``read_file`` target."""

    def listdir(self, path: str) -> list[VolumeFile]:
        prefix = _as_dir_prefix(path)
        entries: list[VolumeFile] = []
        with self._translate_errors():
            for entry in self._iter_delimited_entries(prefix):
                child = entry.name[len(prefix) :]
                if entry.is_directory:
                    if child:
                        entries.append(VolumeFile(path=child, file_type=FileType.DIRECTORY, mtime=0, size=0))
                    continue
                # Skip the prefix placeholder key itself and any deeper descendant.
                if not child or "/" in child:
                    continue
                entries.append(VolumeFile(path=child, file_type=FileType.FILE, mtime=entry.mtime, size=entry.size))
        return entries

    def path_exists(self, path: str) -> bool:
        # A file exists if the exact key exists; a directory exists if any key
        # shares its ``dir/`` prefix. Probing the bare key directly (rather than a
        # single list on the bare prefix) avoids a lexicographically-earlier
        # sibling (e.g. ``foobar`` when probing dir ``foo``) masking the result.
        key = path.lstrip("/")
        dir_prefix = _as_dir_prefix(path)
        with self._translate_errors():
            if dir_prefix and self._prefix_has_any_object(dir_prefix):
                return True
            return self._has_object_at_key(key)

    def read_file(self, path: str) -> bytes:
        key = path.lstrip("/")
        try:
            with self._translate_errors():
                return self._read_object_bytes(key)
        except self._bucket_error_type as e:
            if self._is_not_found(e):
                raise self._make_missing_file_error(path) from e
            raise

    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        if recursive:
            self.remove_directory(path)
            return
        # A missing object is a no-op (idempotent delete); S3 delete never raises
        # not-found, while Azure's does and must be swallowed here.
        try:
            with self._translate_errors():
                self._delete_single_object(path.lstrip("/"))
        except self._bucket_error_type as e:
            if self._is_not_found(e):
                return
            raise

    def remove_directory(self, path: str) -> None:
        with self._translate_errors():
            self._delete_prefix(_as_dir_prefix(path))

    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        with self._translate_errors():
            for path, content in file_contents_by_path.items():
                self._write_object(path.lstrip("/"), content)
