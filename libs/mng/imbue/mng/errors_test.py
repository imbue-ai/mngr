"""Tests for error classes."""

import click
from click.testing import CliRunner

from imbue.mng.errors import AgentNotFoundError
from imbue.mng.errors import AgentNotFoundOnHostError
from imbue.mng.errors import AgentStartError
from imbue.mng.errors import HostDataSchemaError
from imbue.mng.errors import HostNameConflictError
from imbue.mng.errors import HostNotFoundError
from imbue.mng.errors import HostNotRunningError
from imbue.mng.errors import HostNotStoppedError
from imbue.mng.errors import ImageNotFoundError
from imbue.mng.errors import MngError
from imbue.mng.errors import ProviderInstanceNotFoundError
from imbue.mng.errors import ProviderNotAuthorizedError
from imbue.mng.errors import SendMessageError
from imbue.mng.errors import SnapshotNotFoundError
from imbue.mng.errors import SnapshotsNotSupportedError
from imbue.mng.errors import TagLimitExceededError
from imbue.mng.errors import UserInputError
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostState
from imbue.mng.primitives import ImageReference
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotId


def test_agent_not_found_error_sets_agent_identifier() -> None:
    """AgentNotFoundError should set agent_identifier attribute."""
    agent_id = AgentId.generate()
    error = AgentNotFoundError(str(agent_id))
    assert error.agent_identifier == str(agent_id)
    assert str(agent_id) in str(error)


def test_host_not_found_error_with_host_id() -> None:
    """HostNotFoundError should work with HostId."""
    host_id = HostId.generate()
    error = HostNotFoundError(host_id)
    assert error.host == host_id
    assert "Host not found" in str(error)


def test_host_not_found_error_with_host_name() -> None:
    """HostNotFoundError should work with HostName."""
    host_name = HostName("test-host")
    error = HostNotFoundError(host_name)
    assert error.host == host_name
    assert "Host not found" in str(error)


def test_image_not_found_error_sets_image() -> None:
    """ImageNotFoundError should set image attribute."""
    image = ImageReference("nonexistent:tag")
    error = ImageNotFoundError(image)
    assert error.image == image
    assert "Image not found" in str(error)


def test_host_name_conflict_error_sets_name() -> None:
    """HostNameConflictError should set name attribute."""
    name = HostName("duplicate")
    error = HostNameConflictError(name)
    assert error.name == name
    assert "already exists" in str(error)


def test_host_not_running_error_includes_state() -> None:
    """HostNotRunningError should include state in message."""
    host_id = HostId.generate()
    error = HostNotRunningError(host_id, HostState.STOPPED)
    assert error.host_id == host_id
    assert error.state == HostState.STOPPED
    assert HostState.STOPPED.value in str(error)


def test_host_not_stopped_error_includes_state() -> None:
    """HostNotStoppedError should include state in message."""
    host_id = HostId.generate()
    error = HostNotStoppedError(host_id, HostState.RUNNING)
    assert error.host_id == host_id
    assert error.state == HostState.RUNNING
    assert HostState.RUNNING.value in str(error)


def test_snapshot_not_found_error_sets_snapshot_id() -> None:
    """SnapshotNotFoundError should set snapshot_id attribute."""
    snapshot_id = SnapshotId("snap-test")
    error = SnapshotNotFoundError(snapshot_id)
    assert error.snapshot_id == snapshot_id
    assert "Snapshot not found" in str(error)


def test_snapshots_not_supported_error_includes_provider() -> None:
    """SnapshotsNotSupportedError should include provider name."""
    provider_name = ProviderInstanceName("test-provider")
    error = SnapshotsNotSupportedError(provider_name)
    assert error.provider_name == provider_name
    assert "test-provider" in str(error)


def test_tag_limit_exceeded_error_includes_limit_and_actual() -> None:
    """TagLimitExceededError should include both limit and actual."""
    error = TagLimitExceededError(limit=10, actual=15)
    assert error.limit == 10
    assert error.actual == 15
    assert "10" in str(error)
    assert "15" in str(error)


def test_agent_not_found_on_host_error_sets_both_ids() -> None:
    """AgentNotFoundOnHostError should set agent_id and host_id attributes."""
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    error = AgentNotFoundOnHostError(agent_id, host_id)
    assert error.agent_id == agent_id
    assert error.host_id == host_id
    assert str(agent_id) in str(error)
    assert str(host_id) in str(error)


def test_provider_instance_not_found_error_sets_provider_name() -> None:
    """ProviderInstanceNotFoundError should set provider_name attribute."""
    provider_name = ProviderInstanceName("test-provider")
    error = ProviderInstanceNotFoundError(provider_name)
    assert error.provider_name == provider_name
    assert "test-provider" in str(error)


def test_mng_error_has_user_help_text_attribute() -> None:
    """MngError base class should have user_help_text attribute."""
    error = MngError("test error")
    assert hasattr(error, "user_help_text")
    assert error.user_help_text is None


def test_user_input_error_has_user_help_text() -> None:
    """UserInputError should have user_help_text for CLI help."""
    error = UserInputError("invalid input")
    assert error.user_help_text is not None
    assert "mng --help" in error.user_help_text


def test_agent_not_found_error_has_user_help_text() -> None:
    """AgentNotFoundError should have user_help_text for listing agents."""
    agent_id = AgentId.generate()
    error = AgentNotFoundError(str(agent_id))
    assert error.user_help_text is not None
    assert "mng list" in error.user_help_text


def test_host_not_found_error_has_user_help_text() -> None:
    """HostNotFoundError should have user_help_text."""
    host_name = HostName("test-host")
    error = HostNotFoundError(host_name)
    assert error.user_help_text is not None
    assert "mng list" in error.user_help_text


def test_host_name_conflict_error_has_user_help_text() -> None:
    """HostNameConflictError should have user_help_text."""
    name = HostName("duplicate")
    error = HostNameConflictError(name)
    assert error.user_help_text is not None
    assert "mng destroy" in error.user_help_text


def test_host_not_running_error_has_user_help_text() -> None:
    """HostNotRunningError should have user_help_text."""
    host_id = HostId.generate()
    error = HostNotRunningError(host_id, HostState.STOPPED)
    assert error.user_help_text is not None
    assert "mng start" in error.user_help_text


def test_host_not_stopped_error_has_user_help_text() -> None:
    """HostNotStoppedError should have user_help_text."""
    host_id = HostId.generate()
    error = HostNotStoppedError(host_id, HostState.RUNNING)
    assert error.user_help_text is not None
    assert "mng stop" in error.user_help_text


def test_snapshot_not_found_error_has_user_help_text() -> None:
    """SnapshotNotFoundError should have user_help_text."""
    snapshot_id = SnapshotId("snap-test")
    error = SnapshotNotFoundError(snapshot_id)
    assert error.user_help_text is not None
    assert "snapshot" in error.user_help_text.lower()


def test_provider_instance_not_found_error_has_user_help_text() -> None:
    """ProviderInstanceNotFoundError should have user_help_text."""
    provider_name = ProviderInstanceName("test-provider")
    error = ProviderInstanceNotFoundError(provider_name)
    assert error.user_help_text is not None
    assert "provider" in error.user_help_text.lower()


def test_provider_not_authorized_error_sets_provider_name() -> None:
    """ProviderNotAuthorizedError should set provider_name attribute."""
    provider_name = ProviderInstanceName("modal")
    error = ProviderNotAuthorizedError(provider_name)
    assert error.provider_name == provider_name
    assert "not authorized" in str(error).lower()


def test_provider_not_authorized_error_includes_auth_help() -> None:
    """ProviderNotAuthorizedError should include auth_help in message when provided."""
    provider_name = ProviderInstanceName("modal")
    auth_help = "Run 'modal token set' to authenticate."
    error = ProviderNotAuthorizedError(provider_name, auth_help=auth_help)
    assert auth_help in str(error)


def test_provider_not_authorized_error_has_user_help_text() -> None:
    """ProviderNotAuthorizedError should have user_help_text with disable instructions."""
    provider_name = ProviderInstanceName("modal")
    error = ProviderNotAuthorizedError(provider_name)
    assert error.user_help_text is not None
    # Should contain instructions to disable the provider
    assert "mng config set" in error.user_help_text
    assert "is_enabled" in error.user_help_text
    assert "enabled_backends" in error.user_help_text


def test_mng_error_displays_single_error_prefix_via_click() -> None:
    """MngError should display exactly one 'Error: ' prefix when shown via Click.

    Click automatically adds 'Error: ' when displaying ClickException subclasses,
    so MngError.format_message() should NOT add its own prefix.
    """

    @click.command()
    def cmd() -> None:
        raise AgentNotFoundError("test-agent")

    runner = CliRunner()
    result = runner.invoke(cmd)

    # Should have exactly one "Error: " prefix, not "Error: Error: "
    assert result.exit_code == 1
    assert result.output.startswith("Error: ")
    assert "Error: Error:" not in result.output
    assert "Agent not found: test-agent" in result.output


def test_host_data_schema_error_includes_path_and_fix() -> None:
    """HostDataSchemaError should include data path and fix instructions."""
    error = HostDataSchemaError("/tmp/host/data.json", "field 'x' missing")
    assert "/tmp/host/data.json" in str(error)
    assert "incompatible schema" in str(error)
    assert "rm /tmp/host/data.json" in str(error)
    assert error.data_path == "/tmp/host/data.json"
    assert error.validation_error == "field 'x' missing"
    assert error.user_help_text is not None
    assert "field 'x' missing" in error.user_help_text


def test_send_message_error_includes_agent_and_reason() -> None:
    """SendMessageError should include agent name and reason."""
    error = SendMessageError("my-agent", "tmux session not found")
    assert error.agent_name == "my-agent"
    assert error.reason == "tmux session not found"
    assert "my-agent" in str(error)
    assert "tmux session not found" in str(error)


def test_agent_start_error_includes_agent_and_reason() -> None:
    """AgentStartError should include agent name and reason."""
    error = AgentStartError("my-agent", "session already exists")
    assert error.agent_name == "my-agent"
    assert error.reason == "session already exists"
    assert "my-agent" in str(error)
    assert "session already exists" in str(error)
