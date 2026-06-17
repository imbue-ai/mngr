import json
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping

from loguru import logger

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import HostId
from imbue.mngr_vps import state_keys


class BaseStateBucket(MutableModel, ABC):
    """Cloud-agnostic record marshalling + key layout for a provider state bucket.

    A provider state bucket holds mngr's control-plane state -- the full host
    record and per-agent records, plus the offline ``host_dir`` mirror -- keyed by
    host id under the shared ``state_keys`` layout. The marshalling (JSON
    encode/decode), key layout, agent-listing, and offline-read volume are
    identical across clouds; only the raw object get/put/list/delete and the
    SDK Volume differ. This base implements the former ONCE in terms of a handful
    of abstract primitives each cloud supplies (S3, Azure Blob, ...).

    Concrete subclasses keep only: SDK client construction/caching, the raw
    primitives below, error translation, the cloud ``Volume``, and the
    bucket/account lifecycle (ensure/exists/delete). They structurally satisfy the
    ``StateBucket`` Protocol in ``host_state_store.py`` (which stays a Protocol so
    ``BucketHostStateStore`` need not import this concrete base).
    """

    @abstractmethod
    def _put_object(self, key: str, body: str) -> None:
        """Write an object's body (UTF-8), overwriting any existing object."""

    @abstractmethod
    def _get_object(self, key: str) -> str | None:
        """Return an object's body (UTF-8), or None if no object exists at ``key``."""

    @abstractmethod
    def _delete_object(self, key: str) -> None:
        """Delete a single object. Idempotent (no error if absent)."""

    @abstractmethod
    def _list_keys(self, prefix: str) -> list[str]:
        """Return every object key under ``prefix``."""

    @abstractmethod
    def _delete_keys(self, keys: list[str]) -> None:
        """Delete the given keys. Idempotent; a no-op on an empty list."""

    @abstractmethod
    def _prefix_has_objects(self, prefix: str) -> bool:
        """Return whether any object exists under ``prefix`` (a cheap probe, capped at one)."""

    @abstractmethod
    def _make_host_dir_volume(self) -> Volume:
        """Return the un-scoped cloud ``Volume`` for this bucket (the base scopes it per host)."""

    @property
    @abstractmethod
    def _store_label(self) -> str:
        """Human-readable bucket/container name for log messages."""

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
        means the instance never pushed its host_dir (e.g. the sync daemon never
        ran, or the instance has no bucket-write identity).
        """
        return self._prefix_has_objects(state_keys.host_dir_prefix(host_id))

    def volume_for_host(self, host_id: HostId) -> Volume:
        """Return a Volume scoped to ``hosts/<host_id_hex>/host_dir/`` for offline reads.

        Reads use the operator's credentials, so no instance identity is required
        to read -- only to push. The returned volume is rooted at the host's
        ``host_dir`` tree, matching how ``OfflineHostWithVolume`` addresses files
        (relative to ``host_dir``).
        """
        host_dir_prefix = state_keys.host_dir_prefix(host_id).rstrip("/")
        return self._make_host_dir_volume().scoped(host_dir_prefix)
