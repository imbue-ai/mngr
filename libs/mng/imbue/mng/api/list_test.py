import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from io import StringIO
from pathlib import Path

from loguru import logger

from imbue.mng.api.list import _warn_on_duplicate_host_names
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.interfaces.data_types import HostInfo
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentReference
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostReference
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.utils.completion_writer import AGENT_COMPLETIONS_CACHE_FILENAME
from imbue.mng.utils.completion_writer import write_agent_names_cache

# =============================================================================
# Helpers
# =============================================================================


def _make_host_info() -> HostInfo:
    return HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )


def _make_agent_info(name: str, host_info: HostInfo) -> AgentInfo:
    return AgentInfo(
        id=AgentId.generate(),
        name=AgentName(name),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )


# =============================================================================
# Completion Cache Write Tests
# =============================================================================


def test_write_agent_names_cache_writes_sorted_names(
    temp_host_dir: Path,
) -> None:
    """write_agent_names_cache should write sorted agent names to the cache file."""
    write_agent_names_cache(temp_host_dir, ["beta-agent", "alpha-agent"])

    cache_path = temp_host_dir / AGENT_COMPLETIONS_CACHE_FILENAME
    assert cache_path.is_file()
    cache_data = json.loads(cache_path.read_text())
    assert cache_data["names"] == ["alpha-agent", "beta-agent"]
    assert "updated_at" in cache_data


def test_write_agent_names_cache_writes_empty_list_for_no_agents(
    temp_host_dir: Path,
) -> None:
    """write_agent_names_cache should write an empty names list when no agents."""
    write_agent_names_cache(temp_host_dir, [])

    cache_path = temp_host_dir / AGENT_COMPLETIONS_CACHE_FILENAME
    assert cache_path.is_file()
    cache_data = json.loads(cache_path.read_text())
    assert cache_data["names"] == []


def test_write_agent_names_cache_deduplicates_names(
    temp_host_dir: Path,
) -> None:
    """write_agent_names_cache should deduplicate agent names."""
    write_agent_names_cache(temp_host_dir, ["same-name", "same-name"])

    cache_path = temp_host_dir / AGENT_COMPLETIONS_CACHE_FILENAME
    cache_data = json.loads(cache_path.read_text())
    assert cache_data["names"] == ["same-name"]


# =============================================================================
# Duplicate Host Name Warning Tests
# =============================================================================


def _make_host_ref(
    host_name: str,
    provider_name: str = "modal",
) -> HostReference:
    return HostReference(
        host_id=HostId.generate(),
        host_name=HostName(host_name),
        provider_name=ProviderInstanceName(provider_name),
    )


def _make_agent_ref(host_id: HostId, provider_name: str = "modal") -> AgentReference:
    return AgentReference(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName(provider_name),
    )


@contextmanager
def _capture_loguru_warnings() -> Iterator[StringIO]:
    """Capture loguru WARNING-level output into a StringIO buffer."""
    log_output = StringIO()
    sink_id = logger.add(log_output, level="WARNING", format="{message}")
    try:
        yield log_output
    finally:
        logger.remove(sink_id)


def test_warn_on_duplicate_host_names_no_warning_for_unique_names() -> None:
    """_warn_on_duplicate_host_names should not warn when all host names are unique."""
    ref_alpha = _make_host_ref("host-alpha")
    ref_beta = _make_host_ref("host-beta")
    ref_gamma = _make_host_ref("host-gamma")
    agents_by_host = {
        ref_alpha: [_make_agent_ref(ref_alpha.host_id)],
        ref_beta: [_make_agent_ref(ref_beta.host_id)],
        ref_gamma: [_make_agent_ref(ref_gamma.host_id)],
    }

    with _capture_loguru_warnings() as log_output:
        _warn_on_duplicate_host_names(agents_by_host)

    assert "Duplicate host name" not in log_output.getvalue()


def test_warn_on_duplicate_host_names_warns_on_duplicate_within_same_provider() -> None:
    """_warn_on_duplicate_host_names should warn when the same name appears twice on the same provider."""
    ref_dup_1 = _make_host_ref("duplicated-name", "modal")
    ref_dup_2 = _make_host_ref("duplicated-name", "modal")
    ref_unique = _make_host_ref("unique-name", "modal")
    agents_by_host = {
        ref_dup_1: [_make_agent_ref(ref_dup_1.host_id)],
        ref_dup_2: [_make_agent_ref(ref_dup_2.host_id)],
        ref_unique: [_make_agent_ref(ref_unique.host_id)],
    }

    with _capture_loguru_warnings() as log_output:
        _warn_on_duplicate_host_names(agents_by_host)

    output = log_output.getvalue()
    assert "Duplicate host name" in output
    assert "duplicated-name" in output
    assert "modal" in output


def test_warn_on_duplicate_host_names_no_warning_for_same_name_on_different_providers() -> None:
    """_warn_on_duplicate_host_names should not warn when the same name exists on different providers."""
    ref_modal = _make_host_ref("shared-name", "modal")
    ref_docker = _make_host_ref("shared-name", "docker")
    agents_by_host = {
        ref_modal: [_make_agent_ref(ref_modal.host_id, "modal")],
        ref_docker: [_make_agent_ref(ref_docker.host_id, "docker")],
    }

    with _capture_loguru_warnings() as log_output:
        _warn_on_duplicate_host_names(agents_by_host)

    assert "Duplicate host name" not in log_output.getvalue()


def test_warn_on_duplicate_host_names_empty_input() -> None:
    """_warn_on_duplicate_host_names should not warn with an empty input."""
    with _capture_loguru_warnings() as log_output:
        _warn_on_duplicate_host_names({})

    assert "Duplicate host name" not in log_output.getvalue()


def test_warn_on_duplicate_host_names_no_warning_when_destroyed_host_shares_name() -> None:
    """_warn_on_duplicate_host_names should not warn when a destroyed host (no agents) shares a name with an active host."""
    ref_destroyed = _make_host_ref("reused-name", "modal")
    ref_active = _make_host_ref("reused-name", "modal")
    agents_by_host: dict[HostReference, list[AgentReference]] = {
        ref_destroyed: [],
        ref_active: [_make_agent_ref(ref_active.host_id)],
    }

    with _capture_loguru_warnings() as log_output:
        _warn_on_duplicate_host_names(agents_by_host)

    assert "Duplicate host name" not in log_output.getvalue()
