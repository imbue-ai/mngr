from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from typing import Protocol
from typing import runtime_checkable

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

    Every offline-capable provider selects exactly one implementation:
    ``BucketHostStateStore`` over an object-storage bucket (AWS S3, Azure Blob)
    or the GCP instance-metadata store. The object-storage bucket is required
    infrastructure: a provider whose bucket has not been provisioned raises an
    actionable error (pointing at its ``prepare`` command) when its state store is
    accessed -- see :func:`missing_state_bucket_error` -- rather than selecting a
    degraded store. Exposing the stores behind one interface lets the provider
    select a store once and stop branching at every call site.

    Bucket reads and writes propagate the backing store's errors: a mirror write
    that failed silently would let a stopped host show stale state, and a
    swallowed read would make it vanish. Removals are idempotent and tolerate an
    already-absent record. ``host_id`` is the only key; an implementation that
    needs the underlying instance/VM resolves it itself (from a cached listing).
    """

    @abstractmethod
    def persist_host_record(self, record: VpsHostRecord) -> None:
        """Mirror the full host record."""

    @abstractmethod
    def delete_host_state(self, host_id: HostId) -> None:
        """Remove all of the host's mirrored state."""

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
        """Reconstruct the host record from the mirror, or None when the host is unknown to the store."""


class HostDirBackend(MutableModel, ABC):
    """The offline ``host_dir`` capability for a provider's stopped hosts.

    A stopped host's ``host_dir`` is readable offline only when the feature is on
    AND a state bucket exists; otherwise it is simply unavailable. The provider
    selects one of these once (a cached property keyed on exactly those two
    conditions), so no call site re-tests them: the bucket-backed implementation
    does the real work, and ``NullHostDirBackend`` is the no-op fallback. This is
    the host_dir sibling of the ``HostStateStore`` select-once strategy.

    Capture is **operator-driven**: at ``mngr stop`` the operator (mngr, already
    SSH-connected and holding the bucket credentials) reads ``host_dir`` off the
    box and uploads it to the bucket via :meth:`capture`. There is no on-box sync
    daemon and no instance/managed identity -- the only credentials involved are
    the operator's own (the same ones that write the state records). The read path
    (:meth:`volume` / :meth:`volume_reference`) serves the captured tree back from
    the bucket with the operator's creds.

    Limitation: a host that idle-self-poweroffs (or crashes) is NOT captured -- no
    operator is involved at that moment, and by design the box holds no bucket
    creds. Only ``mngr stop`` captures the latest ``host_dir``; an idle-stopped
    host's offline ``host_dir`` therefore reflects its last ``mngr stop`` (or is
    empty if it was never stopped that way). The state *records* are unaffected
    (always operator-written).
    """

    @abstractmethod
    def capture(self, host_id: HostId, vps_ip: str) -> None:
        """Read the host's ``host_dir`` off the box and upload it to the bucket.

        Raises on failure so the operator knows the offline ``host_dir`` was not
        captured. The caller invokes this before pausing the instance and pauses in
        a ``finally``, so a capture failure surfaces (failing ``mngr stop``) without
        ever leaving a running instance. The no-op backend never raises.
        """

    @abstractmethod
    def volume_reference(self, host_id: HostId) -> HostVolume | None:
        """Cheap bucket-backed host_dir volume reference (no network probe), or None when unavailable."""

    @abstractmethod
    def volume(self, host_id: HostId) -> HostVolume | None:
        """Bucket-backed host_dir volume with a light existence probe, or None when nothing was captured."""


class NullHostDirBackend(HostDirBackend):
    """No-op host_dir backend: offline ``host_dir`` is unavailable (feature off or no state bucket).

    The fallback half of the select-once strategy. Shared by every provider --
    "no offline host_dir" looks identical regardless of cloud -- so there is one
    null object rather than a per-provider empty subclass.
    """

    def capture(self, host_id: HostId, vps_ip: str) -> None:
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

    Provider-agnostic over a ``StateBucket``. Storage errors propagate -- the
    bucket is required, so a failed mirror write or read must surface rather than
    let a stopped host show stale state or vanish from listings. ``bucket_label``
    names the bucket in error messages (e.g. "S3 state bucket" / "Azure state
    bucket").
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    bucket: StateBucket
    bucket_label: str

    def persist_host_record(self, record: VpsHostRecord) -> None:
        host_id = HostId(record.certified_host_data.host_id)
        self.bucket.write_host_record_json(host_id, record.model_dump_json(indent=2))

    def delete_host_state(self, host_id: HostId) -> None:
        self.bucket.delete_host_state(host_id)

    def persist_agent_record(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        self.bucket.write_agent_record(host_id, agent_id, agent_data)

    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None:
        self.bucket.remove_agent_record(host_id, agent_id)

    def list_agent_records(self, host_id: HostId) -> list[dict]:
        return self.bucket.list_agent_records(host_id)

    def read_host_record(self, host_id: HostId) -> VpsHostRecord | None:
        """Read+parse the host record from the bucket. None only when genuinely absent.

        A storage error propagates (the caller surfaces it per ``--on-error``); a
        malformed record raises rather than returning None, since silently
        dropping it would make an otherwise-known stopped host vanish.
        """
        record_json = self.bucket.read_host_record_json(host_id)
        if record_json is None:
            return None
        try:
            return VpsHostRecord.model_validate_json(record_json)
        except ValueError as e:
            raise MngrError(f"Malformed host record for {host_id} in {self.bucket_label}: {e}") from e


def missing_state_bucket_error(store_label: str, prepare_command: str) -> MngrError:
    """Actionable error for a provider whose required object-storage bucket is absent.

    The offline mirror lives in the state bucket and there is no degraded
    fallback, so a provider raises this the moment its state store is accessed
    without a provisioned bucket -- whether that is an offline read (a stopped
    host cannot be listed or resumed) or a create/label write (the bucket is a
    prerequisite). ``store_label`` names the missing store (e.g. "S3 state
    bucket") and ``prepare_command`` is the command that creates it (e.g.
    ``mngr aws prepare``).
    """
    return MngrError(
        f"The {store_label} has not been provisioned, so offline host state is unavailable "
        f"(a stopped host cannot be listed or resumed). Run `{prepare_command}` to create it."
    )
