"""Unit tests for the workspace-coordinate resolution helpers in ``app``.

Workspace content URLs are keyed by host id while minds' records stay
agent-keyed; these helpers translate between the two coordinates for the
recovery page, /help scoping, and the sharing page's workspace link.
"""

from collections.abc import Iterator
from collections.abc import Mapping

import pytest
from pydantic import Field

from imbue.minds.desktop_client.app import _resolve_workspace_coordinate_to_agent_id
from imbue.minds.desktop_client.app import _workspace_host_coordinate
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.mngr.primitives import AgentId

_AGENT_A = AgentId("agent-" + "a" * 32)
_AGENT_B = AgentId("agent-" + "b" * 32)
_HOST_A = "host-" + "a" * 32
_HOST_B = "host-" + "b" * 32


class _HostAwareResolver(StaticBackendResolver):
    """StaticBackendResolver that also carries the agent -> host coordinate map."""

    host_id_by_agent_id: Mapping[str, str] = Field(
        default_factory=dict, frozen=True, description="Agent id -> host id, mirroring discovery"
    )

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        host_id = self.host_id_by_agent_id.get(str(agent_id))
        if host_id is None:
            return None
        return AgentDisplayInfo(agent_name=str(agent_id), host_id=host_id)


def _resolver(host_id_by_agent_id: Mapping[str, str]) -> _HostAwareResolver:
    return _HostAwareResolver(
        url_by_agent_and_service={aid: {} for aid in host_id_by_agent_id},
        host_id_by_agent_id=host_id_by_agent_id,
    )


def _record(host_id: str, agent_id: str) -> ReplicaRecord:
    return ReplicaRecord(host_id=host_id, agent_id=agent_id)


def test_agent_coordinate_passes_through_without_lookups() -> None:
    resolved = _resolve_workspace_coordinate_to_agent_id(str(_AGENT_A), _resolver({}), [])
    assert resolved == _AGENT_A


def test_malformed_agent_coordinate_resolves_to_none() -> None:
    assert _resolve_workspace_coordinate_to_agent_id("agent-nothex", _resolver({}), []) is None


@pytest.mark.parametrize("workspace_id", ["", "workspace-1", "localhost", "create"])
def test_non_coordinate_strings_resolve_to_none(workspace_id: str) -> None:
    assert _resolve_workspace_coordinate_to_agent_id(workspace_id, _resolver({}), []) is None


def test_host_coordinate_resolves_via_discovery() -> None:
    resolver = _resolver({str(_AGENT_A): _HOST_A, str(_AGENT_B): _HOST_B})
    assert _resolve_workspace_coordinate_to_agent_id(_HOST_B, resolver, []) == _AGENT_B


def test_host_coordinate_resolution_does_not_touch_records_on_a_discovery_hit() -> None:
    """Records are the fallback: a discovery hit must not list them (they can be slow)."""

    def _exploding_records() -> Iterator[ReplicaRecord]:
        raise AssertionError("records must not be listed when discovery resolves the host id")
        # The unreachable yield makes this a generator, so the raise only
        # fires if the records are actually iterated.
        yield

    resolver = _resolver({str(_AGENT_A): _HOST_A})
    resolved = _resolve_workspace_coordinate_to_agent_id(_HOST_A, resolver, _exploding_records())
    assert resolved == _AGENT_A


def test_host_coordinate_falls_back_to_workspace_records() -> None:
    """A stopped host discovery no longer reports still resolves via the record replica."""
    records = [_record(_HOST_A, str(_AGENT_A)), _record(_HOST_B, str(_AGENT_B))]
    resolved = _resolve_workspace_coordinate_to_agent_id(_HOST_B, _resolver({}), records)
    assert resolved == _AGENT_B


def test_host_coordinate_record_fallback_skips_agentless_records() -> None:
    records = [_record(_HOST_A, "")]
    assert _resolve_workspace_coordinate_to_agent_id(_HOST_A, _resolver({}), records) is None


def test_unknown_host_coordinate_resolves_to_none() -> None:
    resolver = _resolver({str(_AGENT_A): _HOST_A})
    assert _resolve_workspace_coordinate_to_agent_id(_HOST_B, resolver, []) is None


def test_workspace_host_coordinate_prefers_discovery() -> None:
    info = AgentDisplayInfo(agent_name=str(_AGENT_A), host_id=_HOST_A)
    assert _workspace_host_coordinate(info, None, str(_AGENT_A)) == _HOST_A


def test_workspace_host_coordinate_empty_for_undiscovered_agent_without_records() -> None:
    assert _workspace_host_coordinate(None, None, str(_AGENT_A)) == ""


def test_workspace_host_coordinate_ignores_non_host_shaped_coordinates() -> None:
    # A local workspace's coordinate is not a host-<hex> id; the sharing API
    # has nothing to key by, so the pane must get '' rather than 'localhost'.
    info = AgentDisplayInfo(agent_name=str(_AGENT_A), host_id="localhost")
    assert _workspace_host_coordinate(info, None, str(_AGENT_A)) == ""
