from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import HostId
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord


class HostStateStore(MutableModel, ABC):
    """The external mirror of a provider's host + agent records, for offline reads.

    A ``VpsDockerProvider`` keeps the authoritative records on the host volume
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
    def persist_host_record(self, record: VpsDockerHostRecord) -> None:
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
    def read_host_record(self, host_id: HostId) -> VpsDockerHostRecord | None:
        """Reconstruct the host record from the mirror, or None when the host is unknown to the store."""
