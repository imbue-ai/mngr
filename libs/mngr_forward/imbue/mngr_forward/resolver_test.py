import io
import json
from pathlib import Path

import pytest

from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr_forward.data_types import ForwardPortStrategy
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.service_map_cache import ServiceMapCache
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.testing import TEST_AGENT_ID_1
from imbue.mngr_forward.testing import TEST_AGENT_ID_2


@pytest.fixture
def ssh_info() -> RemoteSSHInfo:
    return RemoteSSHInfo(
        user="root",
        host="example.modal.run",
        port=22,
        key_path=Path("/tmp/key"),
    )


def test_resolve_returns_none_for_unknown_agent() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    assert resolver.resolve(TEST_AGENT_ID_1) is None


def test_resolve_service_strategy_returns_none_when_url_unknown() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    assert resolver.resolve(TEST_AGENT_ID_1) is None


def test_resolve_service_strategy_returns_url_when_known() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:9100"})
    target = resolver.resolve(TEST_AGENT_ID_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:9100"
    assert target.ssh_info is None


def test_resolve_service_strategy_includes_ssh_info(ssh_info: RemoteSSHInfo) -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:9100"})
    resolver.update_ssh_info(TEST_AGENT_ID_1, ssh_info)
    target = resolver.resolve(TEST_AGENT_ID_1)
    assert target is not None
    assert target.ssh_info == ssh_info


def test_resolve_port_strategy_returns_fixed_url(ssh_info: RemoteSSHInfo) -> None:
    resolver = ForwardResolver(strategy=ForwardPortStrategy(remote_port=PositiveInt(8080)))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_ssh_info(TEST_AGENT_ID_1, ssh_info)
    target = resolver.resolve(TEST_AGENT_ID_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:8080"
    assert target.ssh_info == ssh_info


def test_update_known_agents_drops_state_for_removed() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:1"})
    resolver.update_known_agents((TEST_AGENT_ID_2,))
    assert resolver.resolve(TEST_AGENT_ID_1) is None
    assert resolver.list_known_agent_ids() == (TEST_AGENT_ID_2,)


def test_remove_known_agent_drops_services() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:1"})
    resolver.remove_known_agent(TEST_AGENT_ID_1)
    assert resolver.resolve(TEST_AGENT_ID_1) is None


def test_update_services_emits_resolver_snapshot_envelope() -> None:
    buf = io.StringIO()
    writer = EnvelopeWriter(output=buf)
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        envelope_writer=writer,
    )
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:9100"})
    lines = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    assert any(
        line["stream"] == "forward"
        and line["payload"].get("type") == "resolver_snapshot"
        and line["payload"]["services_by_agent"]
        == {str(TEST_AGENT_ID_1): {"system_interface": "http://127.0.0.1:9100"}}
        for line in lines
    )


def test_update_services_without_envelope_writer_is_silent() -> None:
    # No envelope writer => no emission, no failure. Tested for the path used by
    # existing resolver-only tests and any code path that doesn't need the snapshot.
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:9100"})


def _resolver_snapshot_payloads(buf: io.StringIO) -> list[dict[str, dict[str, str]]]:
    """Extract the ``services_by_agent`` map from each emitted ``resolver_snapshot`` envelope."""
    payloads: list[dict[str, dict[str, str]]] = []
    for line in buf.getvalue().splitlines():
        if not line:
            continue
        envelope = json.loads(line)
        payload = envelope.get("payload", {})
        if payload.get("type") == "resolver_snapshot":
            payloads.append(payload["services_by_agent"])
    return payloads


def test_remove_known_agent_emits_resolver_snapshot_when_services_were_dropped() -> None:
    """Removing an agent that had a services entry emits a resolver_snapshot
    so the consumer-side mirror does not retain a stale entry."""
    buf = io.StringIO()
    writer = EnvelopeWriter(output=buf)
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        envelope_writer=writer,
    )
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:9100"})
    # The update_services call already emitted one snapshot; the remove must emit another.
    resolver.remove_known_agent(TEST_AGENT_ID_1)

    snapshots = _resolver_snapshot_payloads(buf)
    assert len(snapshots) == 2
    assert snapshots[0] == {str(TEST_AGENT_ID_1): {"system_interface": "http://127.0.0.1:9100"}}
    # The post-remove snapshot no longer contains the dropped agent.
    assert snapshots[1] == {}


def test_remove_known_agent_skips_emission_when_no_services_were_dropped() -> None:
    """Removing an agent with no services entry doesn't fire a spurious empty envelope."""
    buf = io.StringIO()
    writer = EnvelopeWriter(output=buf)
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        envelope_writer=writer,
    )
    resolver.add_known_agent(TEST_AGENT_ID_1)
    # No update_services for TEST_AGENT_ID_1 -- so removing it is a metadata-only
    # change. The mirror has nothing to drop, so no envelope should fire.
    resolver.remove_known_agent(TEST_AGENT_ID_1)

    assert _resolver_snapshot_payloads(buf) == []


def test_update_known_agents_emits_resolver_snapshot_for_bulk_drops() -> None:
    """update_known_agents drops services for agents missing from the new set
    and must emit a single snapshot so consumers stay in sync."""
    buf = io.StringIO()
    writer = EnvelopeWriter(output=buf)
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        envelope_writer=writer,
    )
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.add_known_agent(TEST_AGENT_ID_2)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:9100"})
    resolver.update_services(TEST_AGENT_ID_2, {"system_interface": "http://127.0.0.1:9101"})

    # Drop TEST_AGENT_ID_1 from the known set; TEST_AGENT_ID_2 stays.
    resolver.update_known_agents((TEST_AGENT_ID_2,))

    snapshots = _resolver_snapshot_payloads(buf)
    # 2 from the two update_services calls + 1 from the bulk drop.
    assert len(snapshots) == 3
    assert snapshots[-1] == {str(TEST_AGENT_ID_2): {"system_interface": "http://127.0.0.1:9101"}}


def test_update_known_agents_skips_emission_when_no_services_dropped() -> None:
    """A bulk update that doesn't actually drop any services entries is silent."""
    buf = io.StringIO()
    writer = EnvelopeWriter(output=buf)
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        envelope_writer=writer,
    )
    # No services -- only known-agent metadata.
    resolver.update_known_agents((TEST_AGENT_ID_1, TEST_AGENT_ID_2))
    resolver.update_known_agents((TEST_AGENT_ID_2,))

    assert _resolver_snapshot_payloads(buf) == []


def test_initial_discovery_flag() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    assert resolver.has_completed_initial_discovery() is False
    resolver.update_known_agents(())
    assert resolver.has_completed_initial_discovery() is True


# --- last-known service-map cache (fast first-load) -----------------------


def test_seeded_entry_not_served_until_agent_is_known() -> None:
    """A seeded service URL is not routable until discovery confirms the agent.

    This is the safety property that makes seeding a stale cache acceptable:
    ``resolve`` gates on this run's known-agent set, so a cache entry for an
    agent this run does not discover is never served.
    """
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.seed_services({str(TEST_AGENT_ID_1): {"system_interface": "http://127.0.0.1:8000"}})
    assert resolver.resolve(TEST_AGENT_ID_1) is None
    resolver.add_known_agent(TEST_AGENT_ID_1)
    target = resolver.resolve(TEST_AGENT_ID_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:8000"


def test_live_update_overwrites_seeded_service_entry() -> None:
    """The live event stream's full-replace corrects a stale seed."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.seed_services({str(TEST_AGENT_ID_1): {"system_interface": "http://127.0.0.1:8000"}})
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:9999"})
    target = resolver.resolve(TEST_AGENT_ID_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:9999"


def test_update_services_persists_to_cache(tmp_path: Path) -> None:
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        service_map_cache=cache,
    )
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:8000"})
    assert cache.load() == {str(TEST_AGENT_ID_1): {"system_interface": "http://127.0.0.1:8000"}}


def test_remove_known_agent_drops_cache_entry(tmp_path: Path) -> None:
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        service_map_cache=cache,
    )
    resolver.add_known_agent(TEST_AGENT_ID_1)
    resolver.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:8000"})
    resolver.remove_known_agent(TEST_AGENT_ID_1)
    assert cache.load() == {}


def test_persisted_map_seeds_a_fresh_resolver(tmp_path: Path) -> None:
    """End-to-end: one run persists its service map; a fresh run seeds from it and resolves."""
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    first_run = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        service_map_cache=cache,
    )
    first_run.add_known_agent(TEST_AGENT_ID_1)
    first_run.update_services(TEST_AGENT_ID_1, {"system_interface": "http://127.0.0.1:8000"})

    fresh_run = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    fresh_run.seed_services(cache.load())
    # Still gated on discovery: not served until this run marks the agent known.
    assert fresh_run.resolve(TEST_AGENT_ID_1) is None
    fresh_run.add_known_agent(TEST_AGENT_ID_1)
    target = fresh_run.resolve(TEST_AGENT_ID_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:8000"
