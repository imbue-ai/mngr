import json
from pathlib import Path

from imbue.mng.config.agent_cache import AGENT_COMPLETIONS_CACHE_FILENAME
from imbue.mng.config.agent_cache import resolve_identifiers_from_cache
from imbue.mng.config.agent_cache import write_agent_names_cache
from imbue.mng.conftest import build_agents_by_host_from_tuples
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentReference
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostReference
from imbue.mng.primitives import ProviderInstanceName

# =============================================================================
# write_agent_names_cache format tests
# =============================================================================


def test_write_agent_names_cache_produces_agents_and_names_keys(
    tmp_path: Path,
) -> None:
    agents_by_host, _ = build_agents_by_host_from_tuples(
        [
            ("bench-ep-cache4", "modal", "bench-host"),
            ("my-agent", "docker", "my-docker-host"),
        ]
    )
    write_agent_names_cache(tmp_path, agents_by_host)

    cache_path = tmp_path / AGENT_COMPLETIONS_CACHE_FILENAME
    cache_data = json.loads(cache_path.read_text())

    assert "agents" in cache_data
    assert "names" in cache_data
    assert "updated_at" in cache_data
    assert len(cache_data["agents"]) == 2
    assert cache_data["names"] == ["bench-ep-cache4", "my-agent"]


def test_write_agent_names_cache_agents_contain_provider_info(
    tmp_path: Path,
) -> None:
    agents_by_host, _ = build_agents_by_host_from_tuples(
        [
            ("test-agent", "modal", "test-host"),
        ]
    )
    write_agent_names_cache(tmp_path, agents_by_host)

    cache_path = tmp_path / AGENT_COMPLETIONS_CACHE_FILENAME
    cache_data = json.loads(cache_path.read_text())
    entry = cache_data["agents"][0]

    assert entry["name"] == "test-agent"
    assert entry["provider"] == "modal"
    assert entry["host_name"] == "test-host"
    assert "id" in entry
    assert "host_id" in entry


def test_write_agent_names_cache_writes_empty_list_for_no_agents(
    tmp_path: Path,
) -> None:
    """write_agent_names_cache should write an empty names list when no agents."""
    write_agent_names_cache(tmp_path, {})

    cache_path = tmp_path / AGENT_COMPLETIONS_CACHE_FILENAME
    assert cache_path.is_file()
    cache_data = json.loads(cache_path.read_text())
    assert cache_data["names"] == []
    assert cache_data["agents"] == []


def test_write_agent_names_cache_deduplicates_names(
    tmp_path: Path,
) -> None:
    """write_agent_names_cache should deduplicate names in the names list."""
    # Two agents with the same name on different hosts
    host_ref_1 = HostReference(
        host_id=HostId.generate(),
        host_name=HostName("host-1"),
        provider_name=ProviderInstanceName("modal"),
    )
    host_ref_2 = HostReference(
        host_id=HostId.generate(),
        host_name=HostName("host-2"),
        provider_name=ProviderInstanceName("modal"),
    )
    agents_by_host: dict[HostReference, list[AgentReference]] = {
        host_ref_1: [
            AgentReference(
                host_id=host_ref_1.host_id,
                agent_id=AgentId.generate(),
                agent_name=AgentName("same-name"),
                provider_name=ProviderInstanceName("modal"),
            )
        ],
        host_ref_2: [
            AgentReference(
                host_id=host_ref_2.host_id,
                agent_id=AgentId.generate(),
                agent_name=AgentName("same-name"),
                provider_name=ProviderInstanceName("modal"),
            )
        ],
    }
    write_agent_names_cache(tmp_path, agents_by_host)

    cache_path = tmp_path / AGENT_COMPLETIONS_CACHE_FILENAME
    cache_data = json.loads(cache_path.read_text())
    assert cache_data["names"] == ["same-name"]
    # The agents list should have both entries
    assert len(cache_data["agents"]) == 2


# =============================================================================
# resolve_identifiers_from_cache tests
# =============================================================================


def test_resolve_identifiers_from_cache_round_trip_by_name(
    tmp_path: Path,
) -> None:
    agents_by_host, _ = build_agents_by_host_from_tuples(
        [
            ("bench-ep-cache4", "modal", "bench-host"),
            ("my-agent", "docker", "my-docker-host"),
        ]
    )
    write_agent_names_cache(tmp_path, agents_by_host)

    result = resolve_identifiers_from_cache(tmp_path, ["bench-ep-cache4"])

    assert result is not None
    assert len(result) == 1
    assert result[0].name == "bench-ep-cache4"
    assert result[0].provider == "modal"
    assert result[0].host_name == "bench-host"


def test_resolve_identifiers_from_cache_round_trip_by_id(
    tmp_path: Path,
) -> None:
    agents_by_host, ids_by_name = build_agents_by_host_from_tuples(
        [
            ("bench-ep-cache4", "modal", "bench-host"),
            ("my-agent", "docker", "my-docker-host"),
        ]
    )
    write_agent_names_cache(tmp_path, agents_by_host)

    agent_id = str(ids_by_name["my-agent"])
    result = resolve_identifiers_from_cache(tmp_path, [agent_id])

    assert result is not None
    assert len(result) == 1
    assert result[0].name == "my-agent"
    assert result[0].provider == "docker"


def test_resolve_identifiers_from_cache_returns_entries_for_multiple_identifiers(
    tmp_path: Path,
) -> None:
    agents_by_host, _ = build_agents_by_host_from_tuples(
        [
            ("bench-ep-cache4", "modal", "bench-host"),
            ("my-agent", "docker", "my-docker-host"),
        ]
    )
    write_agent_names_cache(tmp_path, agents_by_host)

    result = resolve_identifiers_from_cache(tmp_path, ["bench-ep-cache4", "my-agent"])

    assert result is not None
    assert len(result) == 2
    providers = {entry.provider for entry in result}
    assert providers == {"modal", "docker"}


def test_resolve_identifiers_from_cache_returns_none_for_missing_identifier(
    tmp_path: Path,
) -> None:
    agents_by_host, _ = build_agents_by_host_from_tuples(
        [
            ("bench-ep-cache4", "modal", "bench-host"),
        ]
    )
    write_agent_names_cache(tmp_path, agents_by_host)

    result = resolve_identifiers_from_cache(tmp_path, ["nonexistent-agent"])

    assert result is None


def test_resolve_identifiers_from_cache_returns_none_when_cache_file_missing(
    tmp_path: Path,
) -> None:
    result = resolve_identifiers_from_cache(tmp_path, ["some-agent"])

    assert result is None


def test_resolve_identifiers_from_cache_returns_none_for_corrupt_cache_file(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / AGENT_COMPLETIONS_CACHE_FILENAME
    cache_path.write_text("not valid json {{{")

    result = resolve_identifiers_from_cache(tmp_path, ["some-agent"])

    assert result is None


def test_resolve_identifiers_from_cache_returns_none_when_agents_key_missing(
    tmp_path: Path,
) -> None:
    """Cache without the 'agents' key (old format) should return None."""
    cache_path = tmp_path / AGENT_COMPLETIONS_CACHE_FILENAME
    cache_data = {
        "names": ["bench-ep-cache4"],
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    cache_path.write_text(json.dumps(cache_data))

    result = resolve_identifiers_from_cache(tmp_path, ["bench-ep-cache4"])

    assert result is None


def test_resolve_identifiers_from_cache_returns_none_when_any_identifier_missing(
    tmp_path: Path,
) -> None:
    """If one identifier is found but another is not, the whole result is None."""
    agents_by_host, _ = build_agents_by_host_from_tuples(
        [
            ("found-agent", "modal", "bench-host"),
        ]
    )
    write_agent_names_cache(tmp_path, agents_by_host)

    result = resolve_identifiers_from_cache(tmp_path, ["found-agent", "missing-agent"])

    assert result is None
