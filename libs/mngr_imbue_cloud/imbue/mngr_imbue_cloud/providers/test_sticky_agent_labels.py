"""Integration-level test for imbue_cloud sticky agent labels (husk fix).

Exercises a full provider round-trip against real on-disk persistence -- only
the outer-SSH boundary is stubbed. A successful discovery pass persists the
agents' identity to the per-host state dir; a following unreachable pass loads
it back and re-attaches it; and ``get_host_and_agent_details`` then shapes those
re-attached refs into ``AgentDetails`` that still carry the workspace's name and
``is_primary`` label. This is the end-to-end path that keeps a transiently
unreachable workspace in the sidebar instead of collapsing it to a husk.
"""

from typing import Any

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.data_types import LeasedHostInfo
from imbue.mngr_imbue_cloud.primitives import LeaseDbId
from imbue.mngr_imbue_cloud.providers.instance import ImbueCloudProvider

_PROVIDER_NAME = ProviderInstanceName("imbue-cloud-test")


class _SequencedListingProvider(ImbueCloudProvider):
    """Provider whose only stubbed boundary is the outer-SSH listing collection.

    Each ``discover_hosts_and_agents`` pass pops one canned ``(raw, error,
    is_auth)`` tuple; the persist/load-from-disk logic and the details-shaping
    path run for real against ``mngr_ctx.profile_dir``.
    """

    _lease: LeasedHostInfo | None = None
    _responses: list[tuple[dict[str, Any] | None, str | None, bool]] = []

    def _list_leased_hosts_cached(self) -> list[LeasedHostInfo]:
        return [self._lease] if self._lease is not None else []

    def _collect_listing_raw_via_outer(self, lease: LeasedHostInfo) -> tuple[dict[str, Any] | None, str | None, bool]:
        return self._responses.pop(0)


def _make_lease(host_id: HostId) -> LeasedHostInfo:
    return LeasedHostInfo(
        host_db_id=LeaseDbId("lease-db-id"),
        vps_address="203.0.113.42",
        ssh_port=22,
        ssh_user="root",
        container_ssh_port=2222,
        agent_id=str(AgentId.generate()),
        host_id=str(host_id),
        host_name="leased-host",
        attributes={},
        leased_at="2025-01-01T00:00:00Z",
    )


def test_persist_then_fallback_round_trip_preserves_identity_in_details(temp_mngr_ctx: MngrContext) -> None:
    host_id = HostId.generate()
    lease = _make_lease(host_id)
    primary = {
        "id": str(AgentId.generate()),
        "name": "primary-agent",
        "labels": {"is_primary": "true"},
        "type": "codex",
    }
    live_raw = {
        "container_state": "running",
        "certified_data": {"image": "some-image"},
        "agents": [{"data": primary}],
    }
    provider = _SequencedListingProvider.model_construct(
        name=_PROVIDER_NAME,
        mngr_ctx=temp_mngr_ctx,
        _lease=lease,
        _responses=[(live_raw, None, False), (None, "outer SSH unreachable: connection timed out", False)],
    )

    # First pass: a live listing. This must persist the agent's identity to disk.
    provider.discover_hosts_and_agents(cg=temp_mngr_ctx.concurrency_group)
    assert provider._last_known_agents_path(host_id).is_file()

    # Second pass: outer SSH is unreachable. Discovery re-attaches the cached agent.
    fallback = provider.discover_hosts_and_agents(cg=temp_mngr_ctx.concurrency_group)
    assert len(fallback) == 1
    host_ref, agent_refs = next(iter(fallback.items()))
    assert host_ref.host_state == HostState.UNKNOWN

    # The rich-details path shapes the re-attached refs into AgentDetails without
    # any change of its own -- the cached name and labels flow straight through.
    host_details, agent_details_list = provider.get_host_and_agent_details(host_ref, agent_refs)
    assert host_details.state == HostState.UNKNOWN
    assert host_details.failure_reason is not None
    assert len(agent_details_list) == 1
    agent_details = agent_details_list[0]
    assert str(agent_details.name) == "primary-agent"
    assert agent_details.labels["is_primary"] == "true"
