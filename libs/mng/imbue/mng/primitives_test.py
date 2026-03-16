"""Tests for primitives."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CertifiedDataError
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import DiscoveredAgent
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import InvalidAgentName
from imbue.mng.primitives import Permission
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import default_branch_name


def test_host_name_extracts_provider_name_when_present() -> None:
    """HostName.provider_name should extract provider after dot."""
    host_name = HostName("myhost.docker")
    assert host_name.provider_name == ProviderInstanceName("docker")


def test_host_name_provider_name_is_none_when_no_dot() -> None:
    """HostName.provider_name should be None when no dot in name."""
    host_name = HostName("myhost")
    assert host_name.provider_name is None


def test_host_name_provider_name_returns_none_with_multiple_dots() -> None:
    """HostName.provider_name should return None when more than 2 parts."""
    host_name = HostName("my.host.docker")
    assert host_name.provider_name is None


def test_host_name_short_name_without_provider() -> None:
    """HostName.short_name should return full name when no provider."""
    host_name = HostName("myhost")
    assert host_name.short_name == "myhost"


def test_host_name_short_name_with_provider() -> None:
    """HostName.short_name should return name before dot."""
    host_name = HostName("myhost.docker")
    assert host_name.short_name == "myhost"


# =============================================================================
# DiscoveredAgent property tests
# =============================================================================


def _make_discovered_agent(
    certified_data: dict | None = None,
) -> DiscoveredAgent:
    """Create a DiscoveredAgent with optional certified_data overrides."""
    base_data: dict = {}
    if certified_data is not None:
        base_data.update(certified_data)
    return DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data=base_data,
    )


def test_discovered_agent_agent_type_returns_none_when_missing() -> None:
    """agent_type should return None when not in certified_data."""
    ref = _make_discovered_agent()
    assert ref.agent_type is None


def test_discovered_agent_agent_type_returns_value_when_present() -> None:
    """agent_type should return AgentTypeName when set in certified_data."""
    ref = _make_discovered_agent({"type": "claude"})
    assert ref.agent_type == AgentTypeName("claude")


def test_discovered_agent_work_dir_returns_none_when_missing() -> None:
    """work_dir should return None when not in certified_data."""
    ref = _make_discovered_agent()
    assert ref.work_dir is None


def test_discovered_agent_work_dir_returns_path_when_present() -> None:
    """work_dir should return Path when set in certified_data."""
    ref = _make_discovered_agent({"work_dir": "/tmp/work"})
    assert ref.work_dir == Path("/tmp/work")


def test_discovered_agent_command_returns_none_when_missing() -> None:
    """command should return None when not in certified_data."""
    ref = _make_discovered_agent()
    assert ref.command is None


def test_discovered_agent_command_returns_value_when_present() -> None:
    """command should return CommandString when set in certified_data."""
    ref = _make_discovered_agent({"command": "sleep 100"})
    assert ref.command == CommandString("sleep 100")


def test_discovered_agent_create_time_returns_none_when_missing() -> None:
    """create_time should return None when not in certified_data."""
    ref = _make_discovered_agent()
    assert ref.create_time is None


def test_discovered_agent_create_time_returns_datetime_from_string() -> None:
    """create_time should parse ISO format string from certified_data."""
    ref = _make_discovered_agent({"create_time": "2024-01-15T12:00:00+00:00"})
    assert ref.create_time is not None
    assert ref.create_time.year == 2024


def test_discovered_agent_create_time_returns_datetime_directly() -> None:
    """create_time should return datetime directly when already a datetime."""
    dt = datetime(2024, 6, 15, tzinfo=timezone.utc)
    ref = _make_discovered_agent({"create_time": dt})
    assert ref.create_time == dt


def test_discovered_agent_start_on_boot_defaults_to_false() -> None:
    """start_on_boot should return False when not in certified_data."""
    ref = _make_discovered_agent()
    assert ref.start_on_boot is False


def test_discovered_agent_permissions_returns_empty_when_missing() -> None:
    """permissions should return empty tuple when not in certified_data."""
    ref = _make_discovered_agent()
    assert ref.permissions == ()


def test_discovered_agent_permissions_returns_values() -> None:
    """permissions should return Permission tuple from certified_data."""
    ref = _make_discovered_agent({"permissions": ["read", "write"]})
    assert ref.permissions == (Permission("read"), Permission("write"))


def test_discovered_agent_labels_returns_empty_when_missing() -> None:
    """labels should return empty dict when not in certified_data."""
    ref = _make_discovered_agent()
    assert ref.labels == {}


def test_discovered_agent_labels_returns_values_when_present() -> None:
    """labels should return dict from certified_data when present."""
    ref = _make_discovered_agent({"labels": {"env": "prod", "team": "infra"}})
    assert ref.labels == {"env": "prod", "team": "infra"}


# =============================================================================
# default_branch_name tests
# =============================================================================


def test_default_branch_name_uses_default_prefix() -> None:
    """default_branch_name should use 'mng/' prefix by default."""
    result = default_branch_name(AgentName("my-agent"))
    assert result == "mng/my-agent"


def test_default_branch_name_uses_custom_prefix() -> None:
    """default_branch_name should use the provided prefix."""
    result = default_branch_name(AgentName("my-agent"), prefix="custom/")
    assert result == "custom/my-agent"


# =============================================================================
# AgentName validation tests
# =============================================================================


def test_agent_name_rejects_leading_dash() -> None:
    """AgentName should reject names starting with a dash."""
    with pytest.raises(InvalidAgentName, match="cannot start or end with a dash"):
        AgentName("-bad-name")


def test_agent_name_rejects_trailing_dash() -> None:
    """AgentName should reject names ending with a dash."""
    with pytest.raises(InvalidAgentName, match="cannot start or end with a dash"):
        AgentName("bad-name-")


def test_agent_name_accepts_valid_name() -> None:
    """AgentName should accept names with internal dashes."""
    name = AgentName("good-agent-name")
    assert str(name) == "good-agent-name"


# =============================================================================
# DiscoveredAgent.created_branch_name tests
# =============================================================================


def test_discovered_agent_created_branch_name_returns_none_when_missing() -> None:
    """created_branch_name should return None when not in certified_data."""
    ref = _make_discovered_agent()
    assert ref.created_branch_name is None


def test_discovered_agent_created_branch_name_returns_string_when_present() -> None:
    """created_branch_name should return the string value from certified_data."""
    ref = _make_discovered_agent({"created_branch_name": "mng/my-agent"})
    assert ref.created_branch_name == "mng/my-agent"


def test_discovered_agent_created_branch_name_returns_none_when_explicitly_none() -> None:
    """created_branch_name should return None when explicitly set to None."""
    ref = _make_discovered_agent({"created_branch_name": None})
    assert ref.created_branch_name is None


def test_discovered_agent_start_on_boot_returns_true_when_set() -> None:
    """start_on_boot should return True when set to True in certified_data."""
    ref = _make_discovered_agent({"start_on_boot": True})
    assert ref.start_on_boot is True


def test_discovered_agent_created_branch_name_raises_on_unexpected_type() -> None:
    """created_branch_name should raise CertifiedDataError for non-string non-None values."""
    ref = _make_discovered_agent({"created_branch_name": 42})
    with pytest.raises(CertifiedDataError, match="Expected str or None"):
        _ = ref.created_branch_name
