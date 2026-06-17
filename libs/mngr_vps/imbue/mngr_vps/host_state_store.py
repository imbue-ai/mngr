from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from typing import Protocol
from typing import runtime_checkable

from loguru import logger
from pydantic import ConfigDict

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.volume import HostVolume
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import HostId
from imbue.mngr_vps.host_store import VpsHostRecord


class HostStateStore(MutableModel, ABC):
    """The external mirror of a provider's host + agent records, for offline reads.

    A ``VpsProvider`` keeps the authoritative records on the host volume
    (read over SSH while the host is reachable). This is the *additional* mirror
    that survives the host being stopped/unreachable, so ``mngr list`` /
    ``mngr start`` / ``mngr event`` etc. still work offline.

    Two implementations back it, chosen by the provider: an object-storage bucket
    (full records, no size limit) and the instance/VM tag mirror (compact, the
    no-bucket fallback). Exposing both behind one interface lets the provider
    select a store once and stop branching on bucket-vs-tags at every call site.

    All methods are best-effort and idempotent: mirroring must never break the
    primary on-volume write/destroy path, and removals tolerate an already-absent
    record. ``host_id`` is the only key; an implementation that needs the
    underlying instance/VM resolves it itself (from a cached listing).
    """

    @abstractmethod
    def persist_host_record(self, record: VpsHostRecord) -> None:
        """Mirror the full host record. The tag store is a no-op (the instance's own tags carry it)."""

    @abstractmethod
    def delete_host_state(self, host_id: HostId) -> None:
        """Remove all of the host's mirrored state. The tag store is a no-op (destroying the instance drops its tags)."""

    @abstractmethod
    def persist_agent_record(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        """Mirror a single agent record (upsert)."""

    @abstractmethod
    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None:
        """Remove a single agent's mirrored record."""

    @abstractmethod
    def list_agent_records(self, host_id: HostId) -> list[dict]:
        """Return the host's mirrored agent records (empty when the host is unknown to the store)."""

    @abstractmethod
    def read_host_record(self, host_id: HostId) -> VpsHostRecord | None:
        """Reconstruct the host record from the mirror, or None when the host is unknown to the store.

        The tag store reads the instance's own tags. The bucket store reads its
        ``host_state.json``, falling back to the tag store when absent (so a
        bucket-mode host created before the bucket existed is still reconstructed).
        """


class HostDirBackend(MutableModel, ABC):
    """The offline ``host_dir`` capability for a provider's stopped hosts.

    A stopped host's ``host_dir`` is readable offline only when the feature is on
    AND a state bucket exists; otherwise it is simply unavailable. The provider
    selects one of these once (a cached property keyed on exactly those two
    conditions), so no call site re-tests them: the bucket-backed implementation
    does the real work, and ``NullHostDirBackend`` is the no-op fallback. This is
    the host_dir sibling of the ``HostStateStore`` select-once strategy.

    All methods are best-effort and never raise -- a host_dir failure only costs
    offline readability, never the primary create/stop path.
    """

    @abstractmethod
    def create_identity(self) -> str | None:
        """Bucket-write identity to attach at create (IAM instance profile / managed-identity id), or None."""

    @abstractmethod
    def install_sync(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the on-box periodic host_dir-to-bucket sync daemon."""

    @abstractmethod
    def trigger_final_sync(self, host_id: HostId, vps_ip: str) -> None:
        """Run one final host_dir sync before the instance pauses, so the offline copy is current."""

    @abstractmethod
    def volume_reference(self, host_id: HostId) -> HostVolume | None:
        """Cheap bucket-backed host_dir volume reference (no network probe), or None when unavailable."""

    @abstractmethod
    def volume(self, host_id: HostId) -> HostVolume | None:
        """Bucket-backed host_dir volume with a light existence probe, or None when unavailable."""


class NullHostDirBackend(HostDirBackend):
    """No-op host_dir backend: offline ``host_dir`` is unavailable (feature off or no state bucket).

    The fallback half of the select-once strategy. Shared by every provider --
    "no offline host_dir" looks identical regardless of cloud -- so there is one
    null object rather than a per-provider empty subclass.
    """

    def create_identity(self) -> str | None:
        return None

    def install_sync(self, *, host_id: HostId, vps_ip: str) -> None:
        pass

    def trigger_final_sync(self, host_id: HostId, vps_ip: str) -> None:
        pass

    def volume_reference(self, host_id: HostId) -> HostVolume | None:
        return None

    def volume(self, host_id: HostId) -> HostVolume | None:
        return None


@runtime_checkable
class StateBucket(Protocol):
    """Object-storage backing for a provider's offline host/agent records.

    Both ``S3StateBucket`` and ``BlobStateBucket`` structurally satisfy this, so
    ``BucketHostStateStore`` works over either without knowing the cloud. Each
    method raises the provider's bucket-error exception on a storage failure.
    """

    def write_host_record_json(self, host_id: HostId, record_json: str) -> None: ...

    def read_host_record_json(self, host_id: HostId) -> str | None: ...

    def write_agent_record(self, host_id: HostId, agent_id: str, data: Mapping[str, object]) -> None: ...

    def list_agent_records(self, host_id: HostId) -> list[dict]: ...

    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None: ...

    def delete_host_state(self, host_id: HostId) -> None: ...

    def host_dir_prefix_has_objects(self, host_id: HostId) -> bool: ...

    def volume_for_host(self, host_id: HostId) -> Volume: ...


class BucketHostStateStore(HostStateStore):
    """Bucket-backed mirror: full host + agent records in object storage (no size limit).

    Provider-agnostic over a ``StateBucket``. Every storage call is wrapped so a
    bucket failure is logged and swallowed (mirroring must never break the primary
    on-volume write/destroy path). ``bucket_error_type`` is the bucket's
    operation-failure exception and ``bucket_label`` names the bucket in logs
    (e.g. "S3 state bucket" / "Azure state bucket").
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    bucket: StateBucket
    bucket_error_type: type[MngrError]
    bucket_label: str
    # Tag store consulted when the bucket has no ``host_state.json`` for a host
    # (e.g. created before the bucket existed); None disables the fallback.
    fallback: HostStateStore | None = None

    def persist_host_record(self, record: VpsHostRecord) -> None:
        host_id = HostId(record.certified_host_data.host_id)
        try:
            self.bucket.write_host_record_json(host_id, record.model_dump_json(indent=2))
        except self.bucket_error_type as e:
            logger.warning("Failed to mirror host record for {} to {}: {}", host_id, self.bucket_label, e)

    def delete_host_state(self, host_id: HostId) -> None:
        try:
            self.bucket.delete_host_state(host_id)
        except self.bucket_error_type as e:
            logger.warning("Failed to delete host state for {} from {}: {}", host_id, self.bucket_label, e)

    def persist_agent_record(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        try:
            self.bucket.write_agent_record(host_id, agent_id, agent_data)
        except self.bucket_error_type as e:
            logger.warning("Failed to mirror agent {} for host {} to {}: {}", agent_id, host_id, self.bucket_label, e)

    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None:
        try:
            self.bucket.remove_agent_record(host_id, agent_id)
        except self.bucket_error_type as e:
            logger.warning(
                "Failed to remove agent {} for host {} from {}: {}", agent_id, host_id, self.bucket_label, e
            )

    def list_agent_records(self, host_id: HostId) -> list[dict]:
        return self.bucket.list_agent_records(host_id)

    def read_host_record(self, host_id: HostId) -> VpsHostRecord | None:
        record = self._read_bucket_host_record(host_id)
        if record is None and self.fallback is not None:
            # The bucket yielded no usable record (no host_state.json -- e.g. a host
            # created before the bucket existed -- or a read error / malformed JSON):
            # fall back to the tag store so it is still reconstructed from its tags.
            return self.fallback.read_host_record(host_id)
        return record

    def _read_bucket_host_record(self, host_id: HostId) -> VpsHostRecord | None:
        """Read+parse the host record from the bucket alone (no fallback). None on absent / error / malformed."""
        try:
            record_json = self.bucket.read_host_record_json(host_id)
        except self.bucket_error_type as e:
            logger.warning("Failed to read host record for {} from {}: {}", host_id, self.bucket_label, e)
            return None
        if record_json is None:
            return None
        try:
            return VpsHostRecord.model_validate_json(record_json)
        except ValueError as e:
            logger.warning("Malformed host record for {} in {}: {}", host_id, self.bucket_label, e)
            return None
