import json

import pytest

from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.providers.provider_release_testing import assert_host_resources_match
from imbue.mngr.providers.provider_release_testing import host_from_list_json
from imbue.mngr.providers.provider_release_testing import host_resources_from_list_json


def _list_document(agent_name: str, resource: dict[str, object] | None) -> str:
    """A list document with one agent on an auto-named host (its name is not the agent's)."""
    return json.dumps(
        {"agents": [{"name": agent_name, "host": {"name": "brave-falcon", "resource": resource}}], "errors": []}
    )


def test_host_resources_from_list_json_reads_the_named_agents_host_resource() -> None:
    document = _list_document(
        "my-agent", {"cpu": {"count": 2, "frequency_ghz": None}, "memory_gb": 4.0, "disk_gb": 80.0, "gpu": None}
    )

    resources = host_resources_from_list_json(f"some log line\n{document}\n", "my-agent")

    assert resources == HostResources(cpu=CpuResources(count=2), memory_gb=4.0, disk_gb=80.0)


def test_host_from_list_json_looks_up_by_agent_name_not_host_name() -> None:
    document = _list_document("my-agent", None)

    assert host_from_list_json(document, "my-agent") == {"name": "brave-falcon", "resource": None}
    assert host_from_list_json(document, "brave-falcon") is None


def test_host_resources_from_list_json_is_none_for_an_unlisted_agent() -> None:
    assert host_resources_from_list_json(_list_document("other-agent", None), "my-agent") is None


def test_host_resources_from_list_json_is_none_when_the_host_lists_no_size() -> None:
    assert host_resources_from_list_json(_list_document("my-agent", None), "my-agent") is None


def test_host_resources_from_list_json_is_none_without_a_json_document() -> None:
    assert host_resources_from_list_json("Error: nothing here\n", "my-agent") is None


def test_assert_host_resources_match_tolerates_memory_rounding_but_not_a_different_cpu_count() -> None:
    expected = HostResources(cpu=CpuResources(count=2), memory_gb=8.0, disk_gb=30.0)

    assert_host_resources_match(HostResources(cpu=CpuResources(count=2), memory_gb=7.98, disk_gb=30.0), expected)
    with pytest.raises(AssertionError, match="CPUs"):
        assert_host_resources_match(HostResources(cpu=CpuResources(count=4), memory_gb=8.0, disk_gb=30.0), expected)
    with pytest.raises(AssertionError, match="disk"):
        assert_host_resources_match(HostResources(cpu=CpuResources(count=2), memory_gb=8.0, disk_gb=None), expected)
