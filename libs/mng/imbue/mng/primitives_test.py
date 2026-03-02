"""Tests for primitives."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentReference
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import Permission
from imbue.mng.primitives import ProviderInstanceName


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
# AgentReference property tests
# =============================================================================


def _make_agent_reference(
    certified_data: dict | None = None,
) -> AgentReference:
    """Create an AgentReference with optional certified_data overrides."""
    base_data: dict = {}
    if certified_data is not None:
        base_data.update(certified_data)
    return AgentReference(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data=base_data,
    )


def test_agent_reference_agent_type_returns_none_when_missing() -> None:
    """agent_type should return None when not in certified_data."""
    ref = _make_agent_reference()
    assert ref.agent_type is None


def test_agent_reference_agent_type_returns_value_when_present() -> None:
    """agent_type should return AgentTypeName when set in certified_data."""
    ref = _make_agent_reference({"type": "claude"})
    assert ref.agent_type == AgentTypeName("claude")


def test_agent_reference_work_dir_returns_none_when_missing() -> None:
    """work_dir should return None when not in certified_data."""
    ref = _make_agent_reference()
    assert ref.work_dir is None


def test_agent_reference_work_dir_returns_path_when_present() -> None:
    """work_dir should return Path when set in certified_data."""
    ref = _make_agent_reference({"work_dir": "/tmp/work"})
    assert ref.work_dir == Path("/tmp/work")


def test_agent_reference_command_returns_none_when_missing() -> None:
    """command should return None when not in certified_data."""
    ref = _make_agent_reference()
    assert ref.command is None


def test_agent_reference_command_returns_value_when_present() -> None:
    """command should return CommandString when set in certified_data."""
    ref = _make_agent_reference({"command": "sleep 100"})
    assert ref.command == CommandString("sleep 100")


def test_agent_reference_create_time_returns_none_when_missing() -> None:
    """create_time should return None when not in certified_data."""
    ref = _make_agent_reference()
    assert ref.create_time is None


def test_agent_reference_create_time_returns_datetime_from_string() -> None:
    """create_time should parse ISO format string from certified_data."""
    ref = _make_agent_reference({"create_time": "2024-01-15T12:00:00+00:00"})
    assert ref.create_time is not None
    assert ref.create_time.year == 2024


def test_agent_reference_create_time_returns_datetime_directly() -> None:
    """create_time should return datetime directly when already a datetime."""
    dt = datetime(2024, 6, 15, tzinfo=timezone.utc)
    ref = _make_agent_reference({"create_time": dt})
    assert ref.create_time == dt


def test_agent_reference_start_on_boot_defaults_to_false() -> None:
    """start_on_boot should return False when not in certified_data."""
    ref = _make_agent_reference()
    assert ref.start_on_boot is False


def test_agent_reference_permissions_returns_empty_when_missing() -> None:
    """permissions should return empty tuple when not in certified_data."""
    ref = _make_agent_reference()
    assert ref.permissions == ()


def test_agent_reference_permissions_returns_values() -> None:
    """permissions should return Permission tuple from certified_data."""
    ref = _make_agent_reference({"permissions": ["read", "write"]})
    assert ref.permissions == (Permission("read"), Permission("write"))


def test_agent_reference_labels_returns_empty_when_missing() -> None:
    """labels should return empty dict when not in certified_data."""
    ref = _make_agent_reference()
    assert ref.labels == {}


def test_agent_reference_labels_returns_values_when_present() -> None:
    """labels should return dict from certified_data when present."""
    ref = _make_agent_reference({"labels": {"env": "prod", "team": "infra"}})
    assert ref.labels == {"env": "prod", "team": "infra"}
