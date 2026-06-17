"""Unit tests for usage preservation on destroy and read-back.

The write side is exercised through an ``OfflineHostWithVolume`` (the
volume-backed reader used by the host-destroy path), which copies file-by-file
and so needs no rsync. The read side plants preserved agent directories under
the local host_dir and asserts discovery, filtering, dedup, and the
``gather_usage_snapshots`` fold-in.
"""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from imbue.mngr.api.preservation import get_local_preserved_agent_dir
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import OfflineHostWithVolume
from imbue.mngr.hosts.offline_host import make_readable_offline_host
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_usage.api import _merge_preserved_events
from imbue.mngr_usage.api import gather_usage_snapshots
from imbue.mngr_usage.api import parse_usage_events
from imbue.mngr_usage.data_types import UsageEvent
from imbue.mngr_usage.preservation import discover_preserved_agents
from imbue.mngr_usage.preservation import preserve_agent_usage

_TEST_HOST_ID = "host-" + "0" * 32


def _make_offline_host_with_volume(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext
) -> OfflineHostWithVolume:
    """Build a volume-backed offline host rooted at the local provider's host_dir."""
    now = datetime.now(timezone.utc)
    offline_host = OfflineHost(
        id=local_provider.host_id,
        certified_host_data=CertifiedHostData(
            host_id=str(local_provider.host_id),
            host_name="test-offline-host",
            created_at=now,
            updated_at=now,
        ),
        provider_instance=local_provider,
        mngr_ctx=temp_mngr_ctx,
    )
    host = make_readable_offline_host(offline_host)
    assert isinstance(host, OfflineHostWithVolume)
    return host


def _usage_event(session_id: str, *, used_percentage: float = 50.0, total_cost_usd: float = 1.0) -> dict[str, Any]:
    """A minimal cost_snapshot event that aggregates into a renderable snapshot."""
    return {
        "source": "claude/usage",
        "type": "cost_snapshot",
        "event_id": f"evt-{session_id}",
        "timestamp": "2056-05-08T10:00:00.000000000Z",
        "session_id": session_id,
        "cost": {"total_cost_usd": total_cost_usd},
        "rate_limits": {"five_hour": {"used_percentage": used_percentage, "resets_at": 9_999_999_999_999}},
    }


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events))


def _data_json(agent_id: AgentId, name: str, *, project: str | None = None) -> dict[str, Any]:
    labels = {"project": project} if project is not None else {}
    return {
        "id": str(agent_id),
        "name": name,
        "type": "claude",
        "work_dir": "/tmp/work",
        "create_time": "2026-02-26T04:29:19.093420+00:00",
        "command": "sleep 9999",
        "labels": labels,
    }


def _plant_volume_usage_agent(volume_root: Path, agent_id: AgentId, events: list[dict[str, Any]]) -> None:
    """Write data.json + usage events into an agent state dir on the volume."""
    agent_dir = volume_root / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "data.json").write_text(json.dumps(_data_json(agent_id, "vol-agent")))
    _write_jsonl(agent_dir / "events" / "claude" / "usage" / "events.jsonl", events)


def _plant_preserved_agent(
    mngr_ctx: MngrContext,
    agent_id: AgentId,
    name: str,
    *,
    provider_name: str = "local",
    project: str | None = None,
    events: list[dict[str, Any]] | None = None,
    write_meta: bool = True,
) -> Path:
    """Plant a preserved agent dir (data.json + meta sidecar + usage events)."""
    dest = get_local_preserved_agent_dir(mngr_ctx, AgentName(name), agent_id)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "data.json").write_text(json.dumps(_data_json(agent_id, name, project=project)))
    if write_meta:
        (dest / "mngr_usage_meta.json").write_text(
            json.dumps({"provider_name": provider_name, "host_id": _TEST_HOST_ID, "host_name": "host1"})
        )
    _write_jsonl(dest / "events" / "claude" / "usage" / "events.jsonl", events or [_usage_event("s1")])
    return dest


# =============================================================================
# Write side
# =============================================================================


def test_preserve_agent_usage_copies_events_and_data_json_and_meta(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    agent_id = AgentId.generate()
    agent_name = AgentName("vol-agent")
    host = _make_offline_host_with_volume(local_provider, temp_mngr_ctx)
    _plant_volume_usage_agent(host.host_dir, agent_id, [_usage_event("s1"), _usage_event("s2")])

    preserve_agent_usage(
        host,
        get_agent_state_dir_path(host.host_dir, agent_id),
        agent_name,
        agent_id,
        provider_name="local",
        host_id=_TEST_HOST_ID,
        host_name="host1",
        mngr_ctx=temp_mngr_ctx,
    )

    dest = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    preserved_events = dest / "events" / "claude" / "usage" / "events.jsonl"
    assert preserved_events.exists()
    assert len(preserved_events.read_text().splitlines()) == 2
    assert (dest / "data.json").exists()
    meta = json.loads((dest / "mngr_usage_meta.json").read_text())
    assert meta == {"provider_name": "local", "host_id": _TEST_HOST_ID, "host_name": "host1"}


def test_preserve_agent_usage_is_noop_without_usage_events(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """An agent with no events/*/usage dir produces no preserved dir at all."""
    agent_id = AgentId.generate()
    agent_name = AgentName("no-usage-agent")
    host = _make_offline_host_with_volume(local_provider, temp_mngr_ctx)
    agent_dir = host.host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "data.json").write_text(json.dumps(_data_json(agent_id, "no-usage-agent")))

    preserve_agent_usage(
        host,
        get_agent_state_dir_path(host.host_dir, agent_id),
        agent_name,
        agent_id,
        provider_name="local",
        host_id=_TEST_HOST_ID,
        host_name="host1",
        mngr_ctx=temp_mngr_ctx,
    )

    assert not get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id).exists()


# =============================================================================
# Read side: discovery + filtering
# =============================================================================


def test_discover_preserved_agents_returns_usage_bearing_dirs(temp_mngr_ctx: MngrContext) -> None:
    agent_id = AgentId.generate()
    _plant_preserved_agent(temp_mngr_ctx, agent_id, "a1")
    refs = discover_preserved_agents(temp_mngr_ctx)
    assert [r.agent_id for r in refs] == [str(agent_id)]


def test_discover_skips_dir_without_usage_meta(temp_mngr_ctx: MngrContext) -> None:
    """A dir preserved by another plugin (no usage sidecar) is ignored."""
    agent_id = AgentId.generate()
    _plant_preserved_agent(temp_mngr_ctx, agent_id, "session-only", write_meta=False)
    assert discover_preserved_agents(temp_mngr_ctx) == []


def test_discover_applies_provider_filter(temp_mngr_ctx: MngrContext) -> None:
    local_id = AgentId.generate()
    remote_id = AgentId.generate()
    _plant_preserved_agent(temp_mngr_ctx, local_id, "local-a", provider_name="local")
    _plant_preserved_agent(temp_mngr_ctx, remote_id, "remote-a", provider_name="modal")

    refs = discover_preserved_agents(temp_mngr_ctx, provider_names=("local",))
    assert [r.agent_id for r in refs] == [str(local_id)]


def test_discover_applies_cel_project_filter(temp_mngr_ctx: MngrContext) -> None:
    foo_id = AgentId.generate()
    bar_id = AgentId.generate()
    _plant_preserved_agent(temp_mngr_ctx, foo_id, "foo-a", project="foo")
    _plant_preserved_agent(temp_mngr_ctx, bar_id, "bar-a", project="bar")

    refs = discover_preserved_agents(temp_mngr_ctx, include_filters=('labels.project == "foo"',))
    assert [r.agent_id for r in refs] == [str(foo_id)]


# =============================================================================
# Read side: merge + gather
# =============================================================================


def test_merge_preserved_events_folds_in_preserved(temp_mngr_ctx: MngrContext) -> None:
    agent_id = AgentId.generate()
    _plant_preserved_agent(temp_mngr_ctx, agent_id, "a1", events=[_usage_event("s1")])

    events_by_source: dict[str, dict[str, list[UsageEvent]]] = {}
    _merge_preserved_events(
        temp_mngr_ctx, events_by_source, include_filters=(), exclude_filters=(), provider_names=None
    )
    assert events_by_source["claude"][str(agent_id)][0].session_id == "s1"


def test_merge_preserved_events_dedups_against_live_agent(temp_mngr_ctx: MngrContext) -> None:
    """A still-live agent that also has a preserved copy is not double-counted."""
    agent_id = AgentId.generate()
    _plant_preserved_agent(temp_mngr_ctx, agent_id, "a1", events=[_usage_event("preserved")])

    events_by_source: dict[str, dict[str, list[UsageEvent]]] = {
        "claude": {str(agent_id): parse_usage_events([_usage_event("live")], "claude")}
    }
    _merge_preserved_events(
        temp_mngr_ctx, events_by_source, include_filters=(), exclude_filters=(), provider_names=None
    )
    sessions = [e.session_id for e in events_by_source["claude"][str(agent_id)]]
    assert sessions == ["live"]


def test_gather_usage_snapshots_includes_preserved_by_default(temp_mngr_ctx: MngrContext) -> None:
    agent_id = AgentId.generate()
    _plant_preserved_agent(temp_mngr_ctx, agent_id, "a1", events=[_usage_event("s1")])

    snapshots = gather_usage_snapshots(
        temp_mngr_ctx,
        now=2_000_000_000,
        include_filters=(),
        exclude_filters=(),
        provider_names=None,
        since_seconds=86_400,
        include_preserved=True,
    )
    assert [s.source_name for s in snapshots] == ["claude"]


def test_gather_usage_snapshots_excludes_preserved_when_disabled(temp_mngr_ctx: MngrContext) -> None:
    agent_id = AgentId.generate()
    _plant_preserved_agent(temp_mngr_ctx, agent_id, "a1", events=[_usage_event("s1")])

    snapshots = gather_usage_snapshots(
        temp_mngr_ctx,
        now=2_000_000_000,
        include_filters=(),
        exclude_filters=(),
        provider_names=None,
        since_seconds=86_400,
        include_preserved=False,
    )
    assert snapshots == []
