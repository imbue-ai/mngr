"""Tests for error classes."""

import io

import click
import pytest
from click.testing import CliRunner

from imbue.mngr.colors import ERROR_COLOR
from imbue.mngr.colors import RESET_COLOR
from imbue.mngr.errors import AgentError
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import CommandTimeoutError
from imbue.mngr.errors import DuplicateAgentNameError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostDataSchemaError
from imbue.mngr.errors import HostError
from imbue.mngr.errors import HostNameConflictError
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import HostNotRunningError
from imbue.mngr.errors import HostNotStoppedError
from imbue.mngr.errors import ImageNotFoundError
from imbue.mngr.errors import LockNotHeldError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import NoCommandDefinedError
from imbue.mngr.errors import ProviderError
from imbue.mngr.errors import ProviderInstanceNotFoundError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.errors import SnapshotNotFoundError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.utils.testing import FakeTtyStream
from imbue.mngr.utils.testing import assert_init_first_param_is_provider_name
from imbue.mngr.utils.testing import walk_concrete_subclasses

_TEST_PROVIDER = ProviderInstanceName("test-provider")


def test_agent_not_found_error_sets_agent_identifier() -> None:
    """AgentNotFoundError should set agent_identifier attribute."""
    agent_id = AgentId.generate()
    error = AgentNotFoundError(str(agent_id))
    assert error.agent_identifier == str(agent_id)
    assert str(agent_id) in str(error)


def test_host_not_found_error_with_host_id() -> None:
    """HostNotFoundError should work with HostId."""
    host_id = HostId.generate()
    error = HostNotFoundError(_TEST_PROVIDER, host_id)
    assert error.provider_name == _TEST_PROVIDER
    assert error.host == host_id
    assert "Host not found" in str(error)


def test_host_not_found_error_with_host_name() -> None:
    """HostNotFoundError should work with HostName."""
    host_name = HostName("test-host")
    error = HostNotFoundError(_TEST_PROVIDER, host_name)
    assert error.provider_name == _TEST_PROVIDER
    assert error.host == host_name
    assert "Host not found" in str(error)


def test_image_not_found_error_sets_image() -> None:
    """ImageNotFoundError should set image attribute."""
    image = ImageReference("nonexistent:tag")
    error = ImageNotFoundError(_TEST_PROVIDER, image)
    assert error.provider_name == _TEST_PROVIDER
    assert error.image == image
    assert "Image not found" in str(error)


def test_host_name_conflict_error_sets_name() -> None:
    """HostNameConflictError should set name attribute."""
    name = HostName("duplicate")
    error = HostNameConflictError(_TEST_PROVIDER, name)
    assert error.provider_name == _TEST_PROVIDER
    assert error.name == name
    assert "already exists" in str(error)


def test_host_not_running_error_includes_state() -> None:
    """HostNotRunningError should include state in message."""
    host_id = HostId.generate()
    error = HostNotRunningError(_TEST_PROVIDER, host_id, HostState.STOPPED)
    assert error.provider_name == _TEST_PROVIDER
    assert error.host_id == host_id
    assert error.state == HostState.STOPPED
    assert HostState.STOPPED.value in str(error)


def test_host_not_stopped_error_includes_state() -> None:
    """HostNotStoppedError should include state in message."""
    host_id = HostId.generate()
    error = HostNotStoppedError(_TEST_PROVIDER, host_id, HostState.RUNNING)
    assert error.provider_name == _TEST_PROVIDER
    assert error.host_id == host_id
    assert error.state == HostState.RUNNING
    assert HostState.RUNNING.value in str(error)


def test_snapshot_not_found_error_sets_snapshot_id() -> None:
    """SnapshotNotFoundError should set snapshot_id attribute."""
    snapshot_id = SnapshotId("snap-test")
    error = SnapshotNotFoundError(_TEST_PROVIDER, snapshot_id)
    assert error.provider_name == _TEST_PROVIDER
    assert error.snapshot_id == snapshot_id
    assert "Snapshot not found" in str(error)


def test_snapshots_not_supported_error_includes_provider() -> None:
    """SnapshotsNotSupportedError should include provider name."""
    provider_name = ProviderInstanceName("test-provider")
    error = SnapshotsNotSupportedError(provider_name)
    assert error.provider_name == provider_name
    assert "test-provider" in str(error)


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


def test_mngr_error_has_user_help_text_attribute() -> None:
    """MngrError base class should have user_help_text attribute."""
    error = MngrError("test error")
    assert hasattr(error, "user_help_text")
    assert error.user_help_text is None


def test_user_input_error_has_user_help_text() -> None:
    """UserInputError should have user_help_text for CLI help."""
    error = UserInputError("invalid input")
    assert error.user_help_text is not None
    assert "mngr --help" in error.user_help_text


def test_agent_not_found_error_has_user_help_text() -> None:
    """AgentNotFoundError should have user_help_text for listing agents."""
    agent_id = AgentId.generate()
    error = AgentNotFoundError(str(agent_id))
    assert error.user_help_text is not None
    assert "mngr list" in error.user_help_text


def test_host_not_found_error_has_user_help_text() -> None:
    """HostNotFoundError should have user_help_text."""
    host_name = HostName("test-host")
    error = HostNotFoundError(_TEST_PROVIDER, host_name)
    assert error.user_help_text is not None
    assert "mngr list" in error.user_help_text


def test_host_name_conflict_error_has_user_help_text() -> None:
    """HostNameConflictError should have user_help_text."""
    name = HostName("duplicate")
    error = HostNameConflictError(_TEST_PROVIDER, name)
    assert error.user_help_text is not None
    assert "mngr destroy" in error.user_help_text


def test_host_not_running_error_has_user_help_text() -> None:
    """HostNotRunningError should have user_help_text."""
    host_id = HostId.generate()
    error = HostNotRunningError(_TEST_PROVIDER, host_id, HostState.STOPPED)
    assert error.user_help_text is not None
    assert "mngr start" in error.user_help_text


def test_host_not_stopped_error_has_user_help_text() -> None:
    """HostNotStoppedError should have user_help_text."""
    host_id = HostId.generate()
    error = HostNotStoppedError(_TEST_PROVIDER, host_id, HostState.RUNNING)
    assert error.user_help_text is not None
    assert "mngr stop" in error.user_help_text


def test_snapshot_not_found_error_has_user_help_text() -> None:
    """SnapshotNotFoundError should have user_help_text."""
    snapshot_id = SnapshotId("snap-test")
    error = SnapshotNotFoundError(_TEST_PROVIDER, snapshot_id)
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
    assert "not authenticated" in str(error).lower()


def test_provider_not_authorized_error_is_provider_unavailable_error() -> None:
    """ProviderNotAuthorizedError should be a ProviderUnavailableError so read paths treat it as unavailable."""
    error = ProviderNotAuthorizedError(ProviderInstanceName("modal"))
    assert isinstance(error, ProviderUnavailableError)


def test_provider_not_authorized_error_includes_reason() -> None:
    """ProviderNotAuthorizedError should include the reason in the message when provided."""
    provider_name = ProviderInstanceName("modal")
    reason = "Modal token missing or invalid"
    error = ProviderNotAuthorizedError(provider_name, reason=reason)
    assert reason in str(error)
    assert error.short_reason == reason


def test_provider_not_authorized_error_carries_short_remediation() -> None:
    """ProviderNotAuthorizedError should expose short_remediation for consistent rendering."""
    error = ProviderNotAuthorizedError(
        ProviderInstanceName("modal"), reason="Modal token missing", short_remediation="run `modal token set`"
    )
    assert error.short_remediation == "run `modal token set`"


def test_provider_not_authorized_error_has_user_help_text() -> None:
    """ProviderNotAuthorizedError should have user_help_text with disable instructions."""
    provider_name = ProviderInstanceName("modal")
    error = ProviderNotAuthorizedError(provider_name)
    assert error.user_help_text is not None
    # Should contain instructions to disable the provider
    assert "mngr config set" in error.user_help_text
    assert "is_enabled" in error.user_help_text
    assert "enabled_backends" in error.user_help_text


def test_mngr_error_displays_single_error_prefix_via_click() -> None:
    """MngrError should display exactly one 'Error: ' prefix when shown via Click.

    Click automatically adds 'Error: ' when displaying ClickException subclasses,
    so MngrError.format_message() should NOT add its own prefix.
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


@pytest.mark.parametrize(
    "host_error_subclass",
    [HostError, HostConnectionError, CommandTimeoutError, LockNotHeldError, HostDataSchemaError],
    ids=lambda c: c.__name__,
)
def test_host_errors_are_mngr_errors(host_error_subclass: type) -> None:
    """HostError and its subclasses are MngrError (and thus ClickException) subclasses.

    This is the single-parent-class consolidation: every host error is now a
    user-facing MngrError, so `except MngrError` handlers catch it and the CLI
    renders it cleanly instead of as a traceback.
    """
    assert issubclass(host_error_subclass, MngrError)
    assert issubclass(host_error_subclass, click.ClickException)


def test_host_connection_error_displays_single_error_prefix_via_click() -> None:
    """A host error raised inside a command renders as a clean 'Error: ' message.

    Before host errors inherited MngrError, an uncaught HostError reached Click
    as a non-ClickException and printed a full traceback. Now Click formats it
    like any other user-facing error.
    """

    @click.command()
    def cmd() -> None:
        raise HostConnectionError("could not reach host")

    runner = CliRunner()
    result = runner.invoke(cmd)

    assert result.exit_code == 1
    assert result.output.startswith("Error: ")
    assert "Error: Error:" not in result.output
    assert "could not reach host" in result.output


@pytest.mark.parametrize(
    "agent_error_subclass",
    [
        AgentError,
        NoCommandDefinedError,
        AgentNotFoundError,
        AgentNotFoundOnHostError,
        SendMessageError,
        DuplicateAgentNameError,
        AgentStartError,
    ],
    ids=lambda c: c.__name__,
)
def test_agent_errors_are_mngr_errors(agent_error_subclass: type) -> None:
    """AgentError and its subclasses are MngrError (and thus ClickException) subclasses.

    This is the single-parent-class consolidation: every agent error is now a
    user-facing MngrError, so `except MngrError` handlers catch it and the CLI
    renders it cleanly instead of as a traceback.
    """
    assert issubclass(agent_error_subclass, MngrError)
    assert issubclass(agent_error_subclass, click.ClickException)


def test_agent_start_error_displays_single_error_prefix_via_click() -> None:
    """An agent error raised inside a command renders as a clean 'Error: ' message.

    Before agent errors inherited MngrError, an uncaught AgentStartError reached
    Click as a non-ClickException and printed a full traceback. Now Click formats
    it like any other user-facing error.
    """

    @click.command()
    def cmd() -> None:
        raise AgentStartError("my-agent", "session already exists")

    runner = CliRunner()
    result = runner.invoke(cmd)

    assert result.exit_code == 1
    assert result.output.startswith("Error: ")
    assert "Error: Error:" not in result.output
    assert "my-agent" in result.output


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


@pytest.mark.parametrize("subclass", walk_concrete_subclasses(ProviderError), ids=lambda c: c.__name__)
def test_provider_error_subclass_takes_provider_name_first(subclass: type) -> None:
    """Every ProviderError subclass must accept provider_name as its first parameter.

    Enforces the contract declared on ProviderError: handlers that catch the base
    class can rely on e.provider_name being present.
    """
    assert_init_first_param_is_provider_name(subclass)


def test_show_colors_error_prefix_on_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a color-capable terminal the whole ``Error:`` line is wrapped in ERROR_COLOR.

    This is the visual-flag fix: an actionable failure (e.g. "run mngr gcp prepare
    first") used to print in the same color as normal output, so it blended in.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = FakeTtyStream()
    MngrError("run mngr gcp prepare first").show(file=stream)
    assert stream.getvalue() == f"{ERROR_COLOR}Error: run mngr gcp prepare first{RESET_COLOR}\n"


def test_show_is_plain_when_not_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Piped output (non-TTY) stays uncolored so captured logs are clean."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = io.StringIO()
    MngrError("boom").show(file=stream)
    assert stream.getvalue() == "Error: boom\n"
    assert ERROR_COLOR not in stream.getvalue()


def test_show_is_plain_when_no_color_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The NO_COLOR convention disables color even on a TTY."""
    monkeypatch.setenv("NO_COLOR", "")
    stream = FakeTtyStream()
    MngrError("boom").show(file=stream)
    assert stream.getvalue() == "Error: boom\n"


def test_show_includes_user_help_text_inside_colored_span(monkeypatch: pytest.MonkeyPatch) -> None:
    """``user_help_text`` is appended via format_message and stays inside the colored span."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = FakeTtyStream()
    UserInputError("bad flag").show(file=stream)
    rendered = stream.getvalue()
    assert rendered.startswith(ERROR_COLOR)
    assert rendered.endswith(f"{RESET_COLOR}\n")
    assert "Error: bad flag  [" in rendered
    assert "mngr --help" in rendered
