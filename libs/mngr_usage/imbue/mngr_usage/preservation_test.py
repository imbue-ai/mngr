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

from imbue.mngr.api.preservation import PRESERVATION_MANIFEST_FILENAME
from imbue.mngr.api.preservation import PreservationManifest
from imbue.mngr.api.preservation import PreservationOutcome
from imbue.mngr.api.preservation import PreservedAgentIdentity
from imbue.mngr.api.preservation import PreservedItemResult
from imbue.mngr.api.preservation import get_local_preserved_agent_dir
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import OfflineHostWithVolume
from imbue.mngr.hosts.offline_host import make_readable_offline_host
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import allow_warnings
from imbue.mngr_usage.api import _merge_preserved_events
from imbue.mngr_usage.api import gather_usage_snapshots
from imbue.mngr_usage.api import parse_usage_events
from imbue.mngr_usage.data_types import UsageEvent
from imbue.mngr_usage.preservation import _write_core_manifest
from imbue.mngr_usage.preservation import discover_preserved_agents
from imbue.mngr_usage.preservation import preserve_agent_usage

_TEST_HOST_ID = HostId("host-" + "0" * 32)


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


def _identity(agent_id: AgentId, name: str, *, labels: dict[str, str] | None = None) -> PreservedAgentIdentity:
    """The provenance a preserved archive records, on the one host these tests destroy from."""
    return PreservedAgentIdentity(
        host_id=_TEST_HOST_ID,
        host_name=HostName("host1"),
        provider_name=ProviderInstanceName("local"),
        agent_id=agent_id,
        agent_name=AgentName(name),
        agent_type=AgentTypeName("claude"),
        labels=labels or {},
    )


def _usage_item(
    outcome: PreservationOutcome = PreservationOutcome.COPIED, error: str | None = None
) -> PreservedItemResult:
    """One copy attempt's result for the usage directory that makes an archive usage-bearing."""
    return PreservedItemResult(rel_path="events/claude/usage", kind=FileType.DIRECTORY, outcome=outcome, error=error)


def _data_item(
    outcome: PreservationOutcome = PreservationOutcome.COPIED, error: str | None = None
) -> PreservedItemResult:
    """One copy attempt's result for the data.json the usage reader reconstructs filters from."""
    return PreservedItemResult(rel_path="data.json", kind=FileType.FILE, outcome=outcome, error=error)


def _write_manifest(dest: Path, identity: PreservedAgentIdentity, *items: PreservedItemResult) -> None:
    (dest / PRESERVATION_MANIFEST_FILENAME).write_text(
        PreservationManifest(identity=identity, items=items).model_dump_json()
    )


# =============================================================================
# Write side
# =============================================================================


def test_preserve_agent_usage_copies_events_and_data_json_and_writes_manifest(
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
        provider_name=ProviderInstanceName("local"),
        host_id=_TEST_HOST_ID,
        host_name=HostName("host1"),
        mngr_ctx=temp_mngr_ctx,
    )

    dest = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    preserved_events = dest / "events" / "claude" / "usage" / "events.jsonl"
    assert preserved_events.exists()
    assert len(preserved_events.read_text().splitlines()) == 2
    assert (dest / "data.json").exists()
    assert not (dest / "mngr_usage_meta.json").exists()
    manifest = PreservationManifest.model_validate_json((dest / PRESERVATION_MANIFEST_FILENAME).read_text())
    assert manifest.identity.provider_name == "local"
    assert manifest.identity.host_id == _TEST_HOST_ID
    assert {(item.rel_path, item.outcome) for item in manifest.items} == {
        ("data.json", "copied"),
        ("events/claude/usage", "copied"),
    }


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
        provider_name=ProviderInstanceName("local"),
        host_id=_TEST_HOST_ID,
        host_name=HostName("host1"),
        mngr_ctx=temp_mngr_ctx,
    )

    assert not get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id).exists()


def test_usage_manifest_does_not_read_stale_data_after_copy_error(tmp_path: Path) -> None:
    stale_id = AgentId.generate()
    (tmp_path / "data.json").write_text(json.dumps(_data_json(stale_id, "stale-agent")))

    _write_core_manifest(
        tmp_path,
        agent_name=AgentName("expected-agent"),
        agent_id=AgentId.generate(),
        provider_name=ProviderInstanceName("local"),
        host_id=_TEST_HOST_ID,
        host_name=HostName("host1"),
        results=(_data_item(PreservationOutcome.ERROR, "read failed"),),
    )

    assert not (tmp_path / PRESERVATION_MANIFEST_FILENAME).exists()


def test_usage_manifest_reports_a_preserved_data_json_it_cannot_use(tmp_path: Path) -> None:
    """An unreadable data.json leaves the archive undiscoverable, so it must not be silent."""
    (tmp_path / "data.json").write_text(json.dumps({"name": "nameless", "type": "claude"}))

    with allow_warnings(match="Ignoring unusable preserved data.json"):
        _write_core_manifest(
            tmp_path,
            agent_name=AgentName("nameless"),
            agent_id=AgentId.generate(),
            provider_name=ProviderInstanceName("local"),
            host_id=_TEST_HOST_ID,
            host_name=HostName("host1"),
            results=(_data_item(),),
        )

    assert not (tmp_path / PRESERVATION_MANIFEST_FILENAME).exists()


def test_usage_manifest_records_retry_failure_with_trusted_existing_identity(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    identity = _identity(agent_id, "retry-agent")
    _write_manifest(tmp_path, identity, _data_item(), _usage_item())
    (tmp_path / "data.json").write_text(json.dumps(_data_json(AgentId.generate(), "stale-agent")))

    _write_core_manifest(
        tmp_path,
        agent_name=identity.agent_name,
        agent_id=agent_id,
        provider_name=ProviderInstanceName("local"),
        host_id=_TEST_HOST_ID,
        host_name=HostName("host1"),
        results=(
            _data_item(PreservationOutcome.ERROR, "read failed"),
            _usage_item(PreservationOutcome.ERROR, "copy failed"),
        ),
    )

    manifest = PreservationManifest.model_validate_json((tmp_path / PRESERVATION_MANIFEST_FILENAME).read_text())
    assert manifest.identity == identity
    assert {(item.rel_path, item.outcome, item.error) for item in manifest.items} == {
        ("data.json", PreservationOutcome.ERROR, "read failed"),
        ("events/claude/usage", PreservationOutcome.ERROR, "copy failed"),
    }


# =============================================================================
# Read side: discovery + filtering
# =============================================================================


def test_discover_preserved_agents_returns_usage_bearing_dirs(temp_mngr_ctx: MngrContext) -> None:
    agent_id = AgentId.generate()
    _plant_preserved_agent(temp_mngr_ctx, agent_id, "a1")
    refs = discover_preserved_agents(temp_mngr_ctx)
    assert [r.agent_id for r in refs] == [str(agent_id)]


def test_discover_preserved_agents_uses_manifest_without_legacy_files(temp_mngr_ctx: MngrContext) -> None:
    agent_id = AgentId.generate()
    dest = _plant_preserved_agent(temp_mngr_ctx, agent_id, "manifest-agent", write_meta=False)
    (dest / "data.json").unlink()
    _write_manifest(dest, _identity(agent_id, "manifest-agent"), _usage_item())

    refs = discover_preserved_agents(temp_mngr_ctx)

    assert [(ref.agent_id, ref.agent_name) for ref in refs] == [(str(agent_id), "manifest-agent")]


def test_discover_preserved_agents_keeps_prior_usage_after_failed_retry(temp_mngr_ctx: MngrContext) -> None:
    agent_id = AgentId.generate()
    dest = _plant_preserved_agent(temp_mngr_ctx, agent_id, "retry-agent", write_meta=False)
    _write_manifest(dest, _identity(agent_id, "retry-agent"), _usage_item(PreservationOutcome.ERROR, "retry failed"))

    refs = discover_preserved_agents(temp_mngr_ctx)

    assert [ref.agent_id for ref in refs] == [str(agent_id)]


def test_discover_manifest_identity_overrides_stale_data_for_filters(temp_mngr_ctx: MngrContext) -> None:
    agent_id = AgentId.generate()
    stale_id = AgentId.generate()
    dest = _plant_preserved_agent(temp_mngr_ctx, stale_id, "stale", provider_name="modal", project="stale")
    _write_manifest(dest, _identity(agent_id, "current", labels={"project": "current"}), _usage_item())

    refs = discover_preserved_agents(
        temp_mngr_ctx,
        provider_names=("local",),
        include_filters=('labels.project == "current"',),
    )

    assert [(ref.agent_id, ref.agent_name) for ref in refs] == [(str(agent_id), "current")]


def test_discover_preserved_agents_skips_an_unusable_manifest_instead_of_falling_back(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A bad manifest supersedes the legacy sidecars, so the archive is left out entirely."""
    agent_id = AgentId.generate()
    dest = _plant_preserved_agent(temp_mngr_ctx, agent_id, "unusable-manifest")
    (dest / PRESERVATION_MANIFEST_FILENAME).write_text("not json")

    with allow_warnings(match="Ignoring invalid preservation manifest"):
        assert discover_preserved_agents(temp_mngr_ctx) == []


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
