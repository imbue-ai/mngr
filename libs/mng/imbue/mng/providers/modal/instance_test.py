import json
from datetime import datetime
from datetime import timezone
from io import StringIO
from pathlib import Path
from typing import Any
from typing import TypeVar
from typing import cast
from unittest.mock import MagicMock
from unittest.mock import patch

import modal
import modal.exception
import pytest

from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import HostNameConflictError
from imbue.mng.errors import MngError
from imbue.mng.errors import ModalAuthError
from imbue.mng.interfaces.data_types import CertifiedHostData
from imbue.mng.interfaces.data_types import SnapshotRecord
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import UserId
from imbue.mng.providers.modal.config import ModalProviderConfig
from imbue.mng.providers.modal.constants import MODAL_TEST_APP_PREFIX
from imbue.mng.providers.modal.instance import HOST_VOLUME_INFIX
from imbue.mng.providers.modal.instance import HostRecord
from imbue.mng.providers.modal.instance import MODAL_VOLUME_NAME_MAX_LENGTH
from imbue.mng.providers.modal.instance import ModalProviderApp
from imbue.mng.providers.modal.instance import ModalProviderInstance
from imbue.mng.providers.modal.instance import SandboxConfig
from imbue.mng.providers.modal.instance import TAG_HOST_ID
from imbue.mng.providers.modal.instance import TAG_HOST_NAME
from imbue.mng.providers.modal.instance import TAG_USER_PREFIX
from imbue.mng.providers.modal.instance import _build_modal_secrets_from_env
from imbue.mng.providers.modal.instance import _parse_volume_spec
from imbue.mng.providers.modal.instance import _substitute_dockerfile_build_args
from imbue.mng.providers.modal.instance import build_sandbox_tags
from imbue.mng.providers.modal.instance import check_host_name_is_unique
from imbue.mng.providers.modal.instance import parse_sandbox_tags

# =============================================================================
# Unit tests for sandbox tag helper functions
# =============================================================================


def test_build_sandbox_tags_with_no_user_tags() -> None:
    """build_sandbox_tags with no user tags should only include host_id and host_name."""
    host_id = HostId.generate()
    name = HostName("test-host")

    tags = build_sandbox_tags(host_id, name, None)

    assert tags == {
        TAG_HOST_ID: str(host_id),
        TAG_HOST_NAME: str(name),
    }


def test_build_sandbox_tags_with_empty_user_tags() -> None:
    """build_sandbox_tags with empty user tags dict should only include host_id and host_name."""
    host_id = HostId.generate()
    name = HostName("test-host")

    tags = build_sandbox_tags(host_id, name, {})

    assert tags == {
        TAG_HOST_ID: str(host_id),
        TAG_HOST_NAME: str(name),
    }


def test_build_sandbox_tags_with_user_tags() -> None:
    """build_sandbox_tags with user tags should prefix them with TAG_USER_PREFIX."""
    host_id = HostId.generate()
    name = HostName("test-host")
    user_tags = {"env": "production", "team": "backend"}

    tags = build_sandbox_tags(host_id, name, user_tags)

    assert tags[TAG_HOST_ID] == str(host_id)
    assert tags[TAG_HOST_NAME] == str(name)
    assert tags[TAG_USER_PREFIX + "env"] == "production"
    assert tags[TAG_USER_PREFIX + "team"] == "backend"
    assert len(tags) == 4


def test_parse_sandbox_tags_extracts_host_id_and_name() -> None:
    """parse_sandbox_tags should extract host_id and name from tags."""
    host_id = HostId.generate()
    name = HostName("test-host")
    tags = {
        TAG_HOST_ID: str(host_id),
        TAG_HOST_NAME: str(name),
    }

    parsed_host_id, parsed_name, parsed_user_tags = parse_sandbox_tags(tags)

    assert parsed_host_id == host_id
    assert parsed_name == name
    assert parsed_user_tags == {}


def test_parse_sandbox_tags_extracts_user_tags() -> None:
    """parse_sandbox_tags should extract user tags and strip the prefix."""
    host_id = HostId.generate()
    name = HostName("test-host")
    tags = {
        TAG_HOST_ID: str(host_id),
        TAG_HOST_NAME: str(name),
        TAG_USER_PREFIX + "env": "staging",
        TAG_USER_PREFIX + "version": "1.0.0",
    }

    parsed_host_id, parsed_name, parsed_user_tags = parse_sandbox_tags(tags)

    assert parsed_host_id == host_id
    assert parsed_name == name
    assert parsed_user_tags == {"env": "staging", "version": "1.0.0"}


def test_build_and_parse_sandbox_tags_roundtrip() -> None:
    """Building and parsing tags should round-trip correctly."""
    host_id = HostId.generate()
    name = HostName("my-test-host")
    user_tags = {"key1": "value1", "key2": "value2"}

    built_tags = build_sandbox_tags(host_id, name, user_tags)
    parsed_host_id, parsed_name, parsed_user_tags = parse_sandbox_tags(built_tags)

    assert parsed_host_id == host_id
    assert parsed_name == name
    assert parsed_user_tags == user_tags


class ExpiredCredentialsModalProviderInstance(ModalProviderInstance):
    """Test subclass that fails on API calls with AuthError.

    This simulates the case where credentials exist but are invalid/expired.
    Used for testing the @handle_modal_auth_error decorator behavior.
    """

    def _get_modal_app(self) -> modal.App:
        raise modal.exception.AuthError("Token missing or expired")


_T = TypeVar("_T", bound=ModalProviderInstance)


def _make_modal_provider_with_mocks(
    mng_ctx: MngContext,
    app_name: str,
    provider_cls: type[_T],
    instance_name: str,
) -> _T:
    """Create a ModalProviderInstance subclass with mocked Modal dependencies for unit tests.

    Uses model_construct() to bypass Pydantic validation, allowing MagicMock objects
    to be used in place of real modal.App and modal.Volume instances.
    """
    mock_app = MagicMock()
    mock_app.app_id = "mock-app-id"
    mock_app.name = app_name

    mock_volume = MagicMock()
    output_buffer = StringIO()
    mock_environment_name = f"test-env-{app_name}"

    modal_app = ModalProviderApp.model_construct(
        app_name=app_name,
        environment_name=mock_environment_name,
        app=mock_app,
        volume=mock_volume,
        close_callback=MagicMock(),
        get_output_callback=output_buffer.getvalue,
    )

    config = ModalProviderConfig(
        app_name=app_name,
        host_dir=Path("/mng"),
        default_sandbox_timeout=300,
        default_cpu=0.5,
        default_memory=0.5,
        is_persistent=False,
        is_snapshotted_after_create=False,
    )

    instance = provider_cls.model_construct(
        name=ProviderInstanceName(instance_name),
        host_dir=Path("/mng"),
        mng_ctx=mng_ctx,
        config=config,
        modal_app=modal_app,
    )
    return instance


def make_modal_provider_with_mocks(mng_ctx: MngContext, app_name: str) -> ModalProviderInstance:
    """Create a ModalProviderInstance with mocked Modal dependencies for unit tests."""
    return _make_modal_provider_with_mocks(mng_ctx, app_name, ModalProviderInstance, "modal-test")


def make_expired_credentials_modal_provider(
    mng_ctx: MngContext, app_name: str
) -> ExpiredCredentialsModalProviderInstance:
    """Create an ExpiredCredentialsModalProviderInstance for testing AuthError handling."""
    return _make_modal_provider_with_mocks(
        mng_ctx, app_name, ExpiredCredentialsModalProviderInstance, "modal-test-expired"
    )


@pytest.fixture
def modal_provider(temp_mng_ctx: MngContext, mng_test_id: str) -> ModalProviderInstance:
    """Create a ModalProviderInstance with mocked Modal for unit/integration tests."""
    app_name = f"{MODAL_TEST_APP_PREFIX}{mng_test_id}"
    return make_modal_provider_with_mocks(temp_mng_ctx, app_name)


@pytest.fixture
def expired_credentials_modal_provider(
    temp_mng_ctx: MngContext, mng_test_id: str
) -> ExpiredCredentialsModalProviderInstance:
    """Create an ExpiredCredentialsModalProviderInstance for testing AuthError handling.

    This provider raises modal.exception.AuthError when API calls are made,
    simulating invalid/expired credentials.
    """
    app_name = f"{MODAL_TEST_APP_PREFIX}{mng_test_id}"
    return make_expired_credentials_modal_provider(temp_mng_ctx, app_name)


# =============================================================================
# Basic property tests (no network required)
# =============================================================================


def test_modal_provider_name(modal_provider: ModalProviderInstance) -> None:
    """Modal provider should have the correct name."""
    assert modal_provider.name == ProviderInstanceName("modal-test")


def test_modal_provider_supports_snapshots(modal_provider: ModalProviderInstance) -> None:
    """Modal provider should support snapshots via sandbox.snapshot_filesystem()."""
    assert modal_provider.supports_snapshots is True


def test_modal_provider_supports_volumes(modal_provider: ModalProviderInstance) -> None:
    """Modal provider should support host volumes."""
    assert modal_provider.supports_volumes is True


def test_modal_provider_supports_mutable_tags(modal_provider: ModalProviderInstance) -> None:
    """Modal provider supports mutable tags via Modal's sandbox.set_tags() API."""
    assert modal_provider.supports_mutable_tags is True


def test_get_host_volume_name_uses_config_prefix(modal_provider: ModalProviderInstance) -> None:
    """Host volume name should use the mng config prefix and host_id hex."""
    host_id = HostId.generate()
    name = modal_provider._get_host_volume_name(host_id)
    expected_prefix = f"{modal_provider.mng_ctx.config.prefix}{HOST_VOLUME_INFIX}"
    assert name.startswith(expected_prefix)
    assert len(name) <= MODAL_VOLUME_NAME_MAX_LENGTH
    assert "host-host-" not in name


def test_volume_id_for_name_produces_valid_volume_id(modal_provider: ModalProviderInstance) -> None:
    """_volume_id_for_name should produce a valid VolumeId from a Modal volume name."""
    vol_name = "mng-vol-abc123def456789012345678abcdef01"
    vol_id = modal_provider._volume_id_for_name(vol_name)
    assert str(vol_id).startswith("vol-")
    assert len(str(vol_id)) == 36


def test_volume_id_for_name_is_deterministic(modal_provider: ModalProviderInstance) -> None:
    """Same volume name should always produce the same VolumeId."""
    vol_name = "test-vol-abc123"
    id1 = modal_provider._volume_id_for_name(vol_name)
    id2 = modal_provider._volume_id_for_name(vol_name)
    assert id1 == id2


def test_volume_id_for_name_different_names_produce_different_ids(
    modal_provider: ModalProviderInstance,
) -> None:
    """Different volume names should produce different VolumeIds."""
    id1 = modal_provider._volume_id_for_name("test-vol-aaa")
    id2 = modal_provider._volume_id_for_name("test-vol-bbb")
    assert id1 != id2


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_handle_modal_auth_error_decorator_converts_auth_error_to_modal_auth_error(
    expired_credentials_modal_provider: ExpiredCredentialsModalProviderInstance,
) -> None:
    """The @handle_modal_auth_error decorator should convert modal.exception.AuthError to ModalAuthError.

    This tests the case where credentials are configured but invalid (e.g., expired token).
    When the actual API call fails with AuthError, the decorator should convert it to
    ModalAuthError.

    Note: We suppress PytestUnhandledThreadExceptionWarning because this test intentionally
    causes an exception in the fetch_sandboxes background thread. The ConcurrencyGroup
    catches and converts this to a ConcurrencyExceptionGroup, which we then convert to
    ModalAuthError. pytest still detects the thread exception as "unhandled" even though
    we properly handle it at the concurrency group level.
    """
    # The expired_credentials_modal_provider raises AuthError when _get_modal_app is
    # called, simulating expired/invalid credentials.
    # list_hosts is decorated with @handle_modal_auth_error
    with pytest.raises(ModalAuthError) as exc_info:
        expired_credentials_modal_provider.list_hosts(cg=expired_credentials_modal_provider.mng_ctx.concurrency_group)

    # Verify the error message contains helpful information
    error_message = str(exc_info.value)
    assert "Modal authentication failed" in error_message
    assert "--disable-plugin modal" in error_message
    assert "https://modal.com/docs/reference/modal.config" in error_message


# =============================================================================
# list_hosts and stopped host tests (unit tests with mocked volume)
# =============================================================================


def _make_host_record(
    host_id: HostId,
    host_name: str = "test-host",
    snapshots: list[SnapshotRecord] | None = None,
    failure_reason: str | None = None,
) -> HostRecord:
    """Create a HostRecord for testing."""
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name=host_name,
        user_tags={},
        snapshots=snapshots or [],
        failure_reason=failure_reason,
        created_at=now,
        updated_at=now,
    )
    return HostRecord(
        ssh_host="test.host",
        ssh_port=22,
        ssh_host_public_key="ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQ...",
        config=SandboxConfig(cpu=1.0, memory=1.0),
        certified_host_data=certified_data,
    )


def _make_snapshot_record(name: str = "initial") -> SnapshotRecord:
    """Create a SnapshotRecord for testing."""
    # The id is now the Modal image ID directly
    return SnapshotRecord(
        id="im-abc123",
        name=name,
        created_at="2026-01-01T00:00:00Z",
    )


def test_list_all_host_records_returns_empty_when_volume_empty(
    modal_provider: ModalProviderInstance,
) -> None:
    """_list_all_host_records should return empty list when volume has no host records."""
    # Mock volume.listdir to return empty
    mock_volume = cast(Any, modal_provider.modal_app.volume)
    mock_volume.listdir.return_value = []

    host_records = modal_provider._list_all_host_records(modal_provider.mng_ctx.concurrency_group)

    assert host_records == []
    mock_volume.listdir.assert_called_once_with("/hosts/")


def test_list_all_host_records_returns_empty_when_hosts_dir_missing(
    modal_provider: ModalProviderInstance,
) -> None:
    """_list_all_host_records should return empty list when /hosts/ directory does not exist."""
    mock_volume = cast(Any, modal_provider.modal_app.volume)
    mock_volume.listdir.side_effect = modal.exception.NotFoundError("Not found")

    host_records = modal_provider._list_all_host_records(modal_provider.mng_ctx.concurrency_group)

    assert host_records == []
    mock_volume.listdir.assert_called_once_with("/hosts/")


def test_list_all_host_records_returns_records_from_volume(
    modal_provider: ModalProviderInstance,
) -> None:
    """_list_all_host_records should return host records from volume."""
    host_id = HostId.generate()
    host_record = _make_host_record(host_id)

    # Mock volume.listdir to return a file entry
    mock_entry = MagicMock()
    mock_entry.path = f"hosts/{host_id}.json"
    mock_volume = cast(Any, modal_provider.modal_app.volume)
    mock_volume.listdir.return_value = [mock_entry]

    # Mock _read_host_record to return the host record
    with patch.object(modal_provider, "_read_host_record", return_value=host_record):
        host_records = modal_provider._list_all_host_records(modal_provider.mng_ctx.concurrency_group)

    assert len(host_records) == 1
    assert host_records[0].certified_host_data.host_id == str(host_id)


def test_list_all_host_records_skips_non_json_files(
    modal_provider: ModalProviderInstance,
) -> None:
    """_list_all_host_records should skip non-.json files."""
    # Mock volume.listdir to return both .json and non-.json files
    mock_entry_json = MagicMock()
    mock_entry_json.path = f"hosts/{HostId.generate()}.json"
    mock_entry_txt = MagicMock()
    mock_entry_txt.path = "hosts/readme.txt"
    mock_entry_dir = MagicMock()
    mock_entry_dir.path = "hosts/subdir"

    mock_volume = cast(Any, modal_provider.modal_app.volume)
    mock_volume.listdir.return_value = [mock_entry_json, mock_entry_txt, mock_entry_dir]

    # Mock _read_host_record - only called for .json files
    with patch.object(modal_provider, "_read_host_record", return_value=None) as mock_read:
        modal_provider._list_all_host_records(modal_provider.mng_ctx.concurrency_group)
        # Should only have tried to read the .json file
        assert mock_read.call_count == 1


def test_list_hosts_includes_running_sandboxes_without_host_records(
    modal_provider: ModalProviderInstance,
) -> None:
    """list_hosts should include running sandboxes even if host record hasn't propagated."""
    host_id = HostId.generate()

    # Mock _list_sandboxes to return a sandbox
    mock_sandbox = MagicMock()
    mock_sandbox.get_tags.return_value = {
        TAG_HOST_ID: str(host_id),
        TAG_HOST_NAME: "test-host",
    }

    # Mock _list_all_host_records to return empty (eventual consistency scenario)
    # Mock _create_host_from_sandbox to return a mock host
    mock_host = MagicMock()
    mock_host.id = host_id

    with (
        patch.object(modal_provider, "_list_sandboxes", return_value=[mock_sandbox]),
        patch.object(modal_provider, "_list_all_host_records", return_value=[]),
        patch.object(modal_provider, "_create_host_from_sandbox", return_value=mock_host),
    ):
        hosts = modal_provider.list_hosts(cg=modal_provider.mng_ctx.concurrency_group)

    assert len(hosts) == 1
    assert hosts[0].id == host_id


def test_list_hosts_returns_stopped_hosts_with_snapshots(
    modal_provider: ModalProviderInstance,
) -> None:
    """list_hosts should return stopped hosts (no sandbox, has snapshots)."""
    host_id = HostId.generate()
    snapshot = _make_snapshot_record("initial")
    host_record = _make_host_record(host_id, snapshots=[snapshot])

    # Mock _list_sandboxes to return empty (no running sandboxes)
    # Mock _list_all_host_records to return the host record with a snapshot
    # Mock _create_host_from_host_record to return a mock host
    mock_host = MagicMock()
    mock_host.id = host_id

    with (
        patch.object(modal_provider, "_list_sandboxes", return_value=[]),
        patch.object(modal_provider, "_list_all_host_records", return_value=[host_record]),
        patch.object(modal_provider, "_create_host_from_host_record", return_value=mock_host),
    ):
        hosts = modal_provider.list_hosts(cg=modal_provider.mng_ctx.concurrency_group)

    assert len(hosts) == 1
    assert hosts[0].id == host_id


def test_list_hosts_excludes_destroyed_hosts_by_default(
    modal_provider: ModalProviderInstance,
) -> None:
    """list_hosts should exclude destroyed hosts (no sandbox, no snapshots) by default."""
    host_id = HostId.generate()
    # Host record with no snapshots = destroyed
    host_record = _make_host_record(host_id, snapshots=[])

    with (
        patch.object(modal_provider, "_list_sandboxes", return_value=[]),
        patch.object(modal_provider, "_list_all_host_records", return_value=[host_record]),
    ):
        hosts = modal_provider.list_hosts(cg=modal_provider.mng_ctx.concurrency_group, include_destroyed=False)

    assert len(hosts) == 0


def test_list_hosts_includes_destroyed_hosts_when_requested(
    modal_provider: ModalProviderInstance,
) -> None:
    """list_hosts(include_destroyed=True) should include destroyed hosts."""
    host_id = HostId.generate()
    # Host record with no snapshots = destroyed
    host_record = _make_host_record(host_id, snapshots=[])

    mock_host = MagicMock()
    mock_host.id = host_id

    with (
        patch.object(modal_provider, "_list_sandboxes", return_value=[]),
        patch.object(modal_provider, "_list_all_host_records", return_value=[host_record]),
        patch.object(modal_provider, "_create_host_from_host_record", return_value=mock_host),
    ):
        hosts = modal_provider.list_hosts(cg=modal_provider.mng_ctx.concurrency_group, include_destroyed=True)

    assert len(hosts) == 1
    assert hosts[0].id == host_id


def test_list_hosts_prefers_running_sandbox_over_host_record(
    modal_provider: ModalProviderInstance,
) -> None:
    """list_hosts should use sandbox for running hosts, not host record."""
    host_id = HostId.generate()
    snapshot = _make_snapshot_record("initial")
    host_record = _make_host_record(host_id, snapshots=[snapshot])

    # Mock sandbox with same host_id
    mock_sandbox = MagicMock()
    mock_sandbox.get_tags.return_value = {
        TAG_HOST_ID: str(host_id),
        TAG_HOST_NAME: "test-host",
    }

    mock_host = MagicMock()
    mock_host.id = host_id

    with (
        patch.object(modal_provider, "_list_sandboxes", return_value=[mock_sandbox]),
        patch.object(modal_provider, "_list_all_host_records", return_value=[host_record]),
        patch.object(modal_provider, "_create_host_from_sandbox", return_value=mock_host) as mock_from_sandbox,
        patch.object(modal_provider, "_create_host_from_host_record") as mock_from_record,
    ):
        hosts = modal_provider.list_hosts(cg=modal_provider.mng_ctx.concurrency_group)

    assert len(hosts) == 1
    # Should use sandbox, not host record
    mock_from_sandbox.assert_called_once()
    mock_from_record.assert_not_called()


# =============================================================================
# Tests for _clear_snapshots_from_host_record
# =============================================================================


def test_clear_snapshots_from_host_record_clears_snapshots(
    modal_provider: ModalProviderInstance,
) -> None:
    """_clear_snapshots_from_host_record should clear snapshots and write updated record."""
    host_id = HostId.generate()
    snapshot = _make_snapshot_record("initial")
    host_record = _make_host_record(host_id, snapshots=[snapshot])

    written_records: list[HostRecord] = []

    def capture_write(record: HostRecord) -> None:
        written_records.append(record)

    with (
        patch.object(modal_provider, "_read_host_record", return_value=host_record),
        patch.object(modal_provider, "_write_host_record", side_effect=capture_write),
    ):
        modal_provider._clear_snapshots_from_host_record(host_id)

    assert len(written_records) == 1
    assert written_records[0].certified_host_data.snapshots == []


def test_clear_snapshots_from_host_record_no_op_when_no_snapshots(
    modal_provider: ModalProviderInstance,
) -> None:
    """_clear_snapshots_from_host_record should not write when there are no snapshots."""
    host_id = HostId.generate()
    host_record = _make_host_record(host_id, snapshots=[])

    with (
        patch.object(modal_provider, "_read_host_record", return_value=host_record),
        patch.object(modal_provider, "_write_host_record") as mock_write,
    ):
        modal_provider._clear_snapshots_from_host_record(host_id)

    mock_write.assert_not_called()


def test_clear_snapshots_from_host_record_no_op_when_no_record(
    modal_provider: ModalProviderInstance,
) -> None:
    """_clear_snapshots_from_host_record should not write when host record doesn't exist."""
    host_id = HostId.generate()

    with (
        patch.object(modal_provider, "_read_host_record", return_value=None),
        patch.object(modal_provider, "_write_host_record") as mock_write,
    ):
        modal_provider._clear_snapshots_from_host_record(host_id)

    mock_write.assert_not_called()


# =============================================================================
# Build args parsing tests (no network required)
# =============================================================================


def test_parse_build_args_empty(modal_provider: ModalProviderInstance) -> None:
    """Empty build args should return default config."""
    config = modal_provider._parse_build_args(None)
    assert config.gpu is None
    # These values come from the modal_provider fixture defaults
    assert config.cpu == 0.5
    assert config.memory == 0.5
    assert config.image is None
    assert config.timeout == 300

    config = modal_provider._parse_build_args([])
    assert config.gpu is None


def test_parse_build_args_key_value_format(modal_provider: ModalProviderInstance) -> None:
    """Should parse simple key=value format."""
    config = modal_provider._parse_build_args(["gpu=h100", "cpu=2", "memory=8"])
    assert config.gpu == "h100"
    assert config.cpu == 2.0
    assert config.memory == 8.0


def test_parse_build_args_flag_equals_format(modal_provider: ModalProviderInstance) -> None:
    """Should parse --key=value format."""
    config = modal_provider._parse_build_args(["--gpu=a100", "--cpu=4", "--memory=16"])
    assert config.gpu == "a100"
    assert config.cpu == 4.0
    assert config.memory == 16.0


def test_parse_build_args_flag_space_format(modal_provider: ModalProviderInstance) -> None:
    """Should parse --key value format (two separate args)."""
    config = modal_provider._parse_build_args(["--gpu", "t4", "--cpu", "1", "--memory", "2"])
    assert config.gpu == "t4"
    assert config.cpu == 1.0
    assert config.memory == 2.0


def test_parse_build_args_mixed_formats(modal_provider: ModalProviderInstance) -> None:
    """Should parse mixed formats in same call."""
    config = modal_provider._parse_build_args(["gpu=h100", "--cpu=2", "--memory", "4"])
    assert config.gpu == "h100"
    assert config.cpu == 2.0
    assert config.memory == 4.0


def test_parse_build_args_image_and_timeout(modal_provider: ModalProviderInstance) -> None:
    """Should parse image and timeout arguments."""
    config = modal_provider._parse_build_args(["image=python:3.11-slim", "timeout=3600"])
    assert config.image == "python:3.11-slim"
    assert config.timeout == 3600


def test_parse_build_args_unknown_raises_error(modal_provider: ModalProviderInstance) -> None:
    """Unknown build args should raise MngError."""
    with pytest.raises(MngError) as exc_info:
        modal_provider._parse_build_args(["gpu=h100", "unknown=value"])
    assert "Unknown build arguments" in str(exc_info.value)


def test_parse_build_args_invalid_type_raises_error(modal_provider: ModalProviderInstance) -> None:
    """Invalid type for numeric args should raise MngError."""
    with pytest.raises(MngError):
        modal_provider._parse_build_args(["cpu=not_a_number"])


def test_parse_build_args_value_with_equals(modal_provider: ModalProviderInstance) -> None:
    """Should handle values containing equals signs."""
    # Image names can contain = in tags
    config = modal_provider._parse_build_args(["--image=myregistry.com/image:tag=v1"])
    assert config.image == "myregistry.com/image:tag=v1"


def test_parse_build_args_region(modal_provider: ModalProviderInstance) -> None:
    """Should parse region argument."""
    config = modal_provider._parse_build_args(["region=us-east"])
    assert config.region == "us-east"

    config = modal_provider._parse_build_args(["--region=eu-west"])
    assert config.region == "eu-west"

    config = modal_provider._parse_build_args(["--region", "us-west"])
    assert config.region == "us-west"


def test_parse_build_args_region_default_is_none(modal_provider: ModalProviderInstance) -> None:
    """Region should default to None (auto-select)."""
    config = modal_provider._parse_build_args([])
    assert config.region is None

    config = modal_provider._parse_build_args(["cpu=2"])
    assert config.region is None


def test_parse_build_args_context_dir(modal_provider: ModalProviderInstance) -> None:
    """Should parse context-dir argument."""
    config = modal_provider._parse_build_args(["context-dir=/path/to/context"])
    assert config.context_dir == "/path/to/context"

    config = modal_provider._parse_build_args(["--context-dir=/another/path"])
    assert config.context_dir == "/another/path"

    config = modal_provider._parse_build_args(["--context-dir", "/third/path"])
    assert config.context_dir == "/third/path"


def test_parse_build_args_context_dir_default_is_none(modal_provider: ModalProviderInstance) -> None:
    """context_dir should default to None (use Dockerfile's directory)."""
    config = modal_provider._parse_build_args([])
    assert config.context_dir is None

    config = modal_provider._parse_build_args(["cpu=2"])
    assert config.context_dir is None


def test_parse_build_args_single_secret(modal_provider: ModalProviderInstance) -> None:
    """Should parse a single --secret argument."""
    config = modal_provider._parse_build_args(["--secret=MY_TOKEN"])
    assert config.secrets == ("MY_TOKEN",)


def test_parse_build_args_multiple_secrets(modal_provider: ModalProviderInstance) -> None:
    """Should parse multiple --secret arguments."""
    config = modal_provider._parse_build_args(["--secret=TOKEN1", "--secret=TOKEN2", "--secret=TOKEN3"])
    assert config.secrets == ("TOKEN1", "TOKEN2", "TOKEN3")


def test_parse_build_args_secret_with_key_value_format(modal_provider: ModalProviderInstance) -> None:
    """Should parse secret=VAR format."""
    config = modal_provider._parse_build_args(["secret=MY_TOKEN"])
    assert config.secrets == ("MY_TOKEN",)


def test_parse_build_args_secret_default_is_empty(modal_provider: ModalProviderInstance) -> None:
    """secrets should default to empty tuple."""
    config = modal_provider._parse_build_args([])
    assert config.secrets == ()

    config = modal_provider._parse_build_args(["cpu=2"])
    assert config.secrets == ()


def test_parse_build_args_secrets_with_other_args(modal_provider: ModalProviderInstance) -> None:
    """Should parse secrets alongside other build args."""
    config = modal_provider._parse_build_args(["cpu=2", "--secret=TOKEN1", "memory=4", "--secret=TOKEN2"])
    assert config.cpu == 2.0
    assert config.memory == 4.0
    assert config.secrets == ("TOKEN1", "TOKEN2")


# =============================================================================
# Build args: --cidr-allowlist and --offline
# =============================================================================


def test_parse_build_args_cidr_allowlist_default_is_empty(modal_provider: ModalProviderInstance) -> None:
    """cidr_allowlist should default to empty tuple."""
    config = modal_provider._parse_build_args([])
    assert config.cidr_allowlist == ()

    config = modal_provider._parse_build_args(["cpu=2"])
    assert config.cidr_allowlist == ()


def test_parse_build_args_single_cidr_allowlist(modal_provider: ModalProviderInstance) -> None:
    """Should parse a single --cidr-allowlist argument."""
    config = modal_provider._parse_build_args(["--cidr-allowlist=203.0.113.0/24"])
    assert config.cidr_allowlist == ("203.0.113.0/24",)


def test_parse_build_args_multiple_cidr_allowlist(modal_provider: ModalProviderInstance) -> None:
    """Should parse multiple --cidr-allowlist arguments."""
    config = modal_provider._parse_build_args(
        [
            "--cidr-allowlist=203.0.113.0/24",
            "--cidr-allowlist=10.0.0.0/8",
        ]
    )
    assert config.cidr_allowlist == ("203.0.113.0/24", "10.0.0.0/8")


def test_parse_build_args_cidr_allowlist_key_value_format(modal_provider: ModalProviderInstance) -> None:
    """Should parse cidr-allowlist=CIDR format."""
    config = modal_provider._parse_build_args(["cidr-allowlist=10.0.0.0/8"])
    assert config.cidr_allowlist == ("10.0.0.0/8",)


def test_parse_build_args_cidr_allowlist_space_format(modal_provider: ModalProviderInstance) -> None:
    """Should parse --cidr-allowlist CIDR format (two separate args)."""
    config = modal_provider._parse_build_args(["--cidr-allowlist", "192.168.0.0/16"])
    assert config.cidr_allowlist == ("192.168.0.0/16",)


def test_parse_build_args_cidr_allowlist_with_other_args(modal_provider: ModalProviderInstance) -> None:
    """Should parse cidr-allowlist alongside other build args."""
    config = modal_provider._parse_build_args(
        [
            "cpu=2",
            "--cidr-allowlist=10.0.0.0/8",
            "memory=4",
            "--cidr-allowlist=172.16.0.0/12",
        ]
    )
    assert config.cpu == 2.0
    assert config.memory == 4.0
    assert config.cidr_allowlist == ("10.0.0.0/8", "172.16.0.0/12")


def test_parse_build_args_offline_default_is_false(modal_provider: ModalProviderInstance) -> None:
    """offline should default to False."""
    config = modal_provider._parse_build_args([])
    assert config.offline is False


def test_parse_build_args_offline_flag(modal_provider: ModalProviderInstance) -> None:
    """--offline flag should set offline to True."""
    config = modal_provider._parse_build_args(["--offline"])
    assert config.offline is True


def test_parse_build_args_offline_bare_word(modal_provider: ModalProviderInstance) -> None:
    """Bare word 'offline' (without --) should be normalized and set offline to True."""
    config = modal_provider._parse_build_args(["offline"])
    assert config.offline is True


def test_parse_build_args_offline_with_other_args(modal_provider: ModalProviderInstance) -> None:
    """--offline should work alongside other build args."""
    config = modal_provider._parse_build_args(["cpu=2", "--offline", "memory=4"])
    assert config.offline is True
    assert config.cpu == 2.0
    assert config.memory == 4.0


def test_effective_cidr_allowlist_default_is_none(modal_provider: ModalProviderInstance) -> None:
    """No --offline or --cidr-allowlist should produce None (allow all)."""
    config = modal_provider._parse_build_args([])
    assert config.effective_cidr_allowlist is None


def test_effective_cidr_allowlist_offline_produces_empty_list(modal_provider: ModalProviderInstance) -> None:
    """--offline should produce an empty list (block all)."""
    config = modal_provider._parse_build_args(["--offline"])
    assert config.effective_cidr_allowlist == []


def test_effective_cidr_allowlist_with_cidrs(modal_provider: ModalProviderInstance) -> None:
    """--cidr-allowlist should produce the specified list."""
    config = modal_provider._parse_build_args(["--cidr-allowlist=10.0.0.0/8"])
    assert config.effective_cidr_allowlist == ["10.0.0.0/8"]


def test_effective_cidr_allowlist_cidrs_override_offline(modal_provider: ModalProviderInstance) -> None:
    """When both --offline and --cidr-allowlist are provided, explicit CIDRs take precedence."""
    config = modal_provider._parse_build_args(["--offline", "--cidr-allowlist=10.0.0.0/8"])
    assert config.effective_cidr_allowlist == ["10.0.0.0/8"]


# =============================================================================
# Tests for volume build args
# =============================================================================


def test_parse_build_args_single_volume(modal_provider: ModalProviderInstance) -> None:
    """Should parse a single --volume argument."""
    config = modal_provider._parse_build_args(["--volume=my-data:/data"])
    assert config.volumes == (("my-data", "/data"),)


def test_parse_build_args_single_volume_key_value_format(modal_provider: ModalProviderInstance) -> None:
    """Should parse volume=name:/path key-value format."""
    config = modal_provider._parse_build_args(["volume=my-data:/data"])
    assert config.volumes == (("my-data", "/data"),)


def test_parse_build_args_multiple_volumes(modal_provider: ModalProviderInstance) -> None:
    """Should parse multiple --volume arguments."""
    config = modal_provider._parse_build_args(["--volume=cache:/cache", "--volume=results:/results"])
    assert config.volumes == (("cache", "/cache"), ("results", "/results"))


def test_parse_build_args_volume_invalid_format_no_colon(modal_provider: ModalProviderInstance) -> None:
    """Missing ':' separator in volume spec should raise MngError."""
    with pytest.raises(MngError) as exc_info:
        modal_provider._parse_build_args(["--volume=nodatapath"])
    assert "Invalid volume spec" in str(exc_info.value)


def test_parse_build_args_volume_invalid_format_empty_name(modal_provider: ModalProviderInstance) -> None:
    """Empty volume name should raise MngError."""
    with pytest.raises(MngError) as exc_info:
        modal_provider._parse_build_args(["--volume=:/data"])
    assert "Invalid volume spec" in str(exc_info.value)


def test_parse_build_args_volume_invalid_format_empty_path(modal_provider: ModalProviderInstance) -> None:
    """Empty mount path should raise MngError."""
    with pytest.raises(MngError) as exc_info:
        modal_provider._parse_build_args(["--volume=name:"])
    assert "Invalid volume spec" in str(exc_info.value)


def test_parse_build_args_volume_default_is_empty(modal_provider: ModalProviderInstance) -> None:
    """volumes should default to empty tuple."""
    config = modal_provider._parse_build_args([])
    assert config.volumes == ()

    config = modal_provider._parse_build_args(["cpu=2"])
    assert config.volumes == ()


def test_parse_build_args_volumes_with_other_args(modal_provider: ModalProviderInstance) -> None:
    """Should parse volumes alongside other build args."""
    config = modal_provider._parse_build_args(["cpu=2", "--volume=data:/data", "memory=4", "--volume=logs:/logs"])
    assert config.cpu == 2.0
    assert config.memory == 4.0
    assert config.volumes == (("data", "/data"), ("logs", "/logs"))


def test_parse_volume_spec_valid() -> None:
    """Should parse valid volume specs."""
    assert _parse_volume_spec("my-vol:/mount") == ("my-vol", "/mount")
    assert _parse_volume_spec("name:/path/to/dir") == ("name", "/path/to/dir")


def test_parse_volume_spec_invalid_no_colon() -> None:
    """Should raise on missing colon."""
    with pytest.raises(MngError):
        _parse_volume_spec("nopath")


def test_parse_volume_spec_invalid_empty_parts() -> None:
    """Should raise on empty name or path."""
    with pytest.raises(MngError):
        _parse_volume_spec(":/path")
    with pytest.raises(MngError):
        _parse_volume_spec("name:")


# =============================================================================
# Tests for config-level defaults in _parse_build_args
# =============================================================================


def make_modal_provider_with_config_defaults(
    mng_ctx: MngContext,
    app_name: str,
    default_gpu: str | None = None,
    default_image: str | None = None,
    default_region: str | None = None,
) -> ModalProviderInstance:
    """Create a ModalProviderInstance with custom config defaults for testing."""
    mock_app = MagicMock()
    mock_app.app_id = "mock-app-id"
    mock_app.name = app_name

    mock_volume = MagicMock()
    output_buffer = StringIO()

    # Create a mock environment name for testing
    mock_environment_name = f"test-env-{app_name}"

    modal_app = ModalProviderApp.model_construct(
        app_name=app_name,
        environment_name=mock_environment_name,
        app=mock_app,
        volume=mock_volume,
        close_callback=MagicMock(),
        get_output_callback=output_buffer.getvalue,
    )

    config = ModalProviderConfig(
        app_name=app_name,
        host_dir=Path("/mng"),
        default_sandbox_timeout=300,
        default_cpu=0.5,
        default_memory=0.5,
        default_gpu=default_gpu,
        default_image=default_image,
        default_region=default_region,
        is_persistent=False,
    )

    instance = ModalProviderInstance.model_construct(
        name=ProviderInstanceName("modal-test"),
        host_dir=Path("/mng"),
        mng_ctx=mng_ctx,
        config=config,
        modal_app=modal_app,
    )
    return instance


def test_parse_build_args_uses_config_default_gpu(temp_mng_ctx: MngContext) -> None:
    """When default_gpu is set in config, _parse_build_args should use it."""
    provider = make_modal_provider_with_config_defaults(
        temp_mng_ctx,
        app_name="test-app",
        default_gpu="h100",
    )
    config = provider._parse_build_args([])
    assert config.gpu == "h100"

    # Empty list should also use default
    config = provider._parse_build_args(None)
    assert config.gpu == "h100"


def test_parse_build_args_uses_config_default_image(temp_mng_ctx: MngContext) -> None:
    """When default_image is set in config, _parse_build_args should use it."""
    provider = make_modal_provider_with_config_defaults(
        temp_mng_ctx,
        app_name="test-app",
        default_image="python:3.11-slim",
    )
    config = provider._parse_build_args([])
    assert config.image == "python:3.11-slim"


def test_parse_build_args_uses_config_default_region(temp_mng_ctx: MngContext) -> None:
    """When default_region is set in config, _parse_build_args should use it."""
    provider = make_modal_provider_with_config_defaults(
        temp_mng_ctx,
        app_name="test-app",
        default_region="us-east",
    )
    config = provider._parse_build_args([])
    assert config.region == "us-east"


def test_parse_build_args_uses_all_config_defaults(temp_mng_ctx: MngContext) -> None:
    """When all defaults are set in config, _parse_build_args should use them."""
    provider = make_modal_provider_with_config_defaults(
        temp_mng_ctx,
        app_name="test-app",
        default_gpu="a100",
        default_image="ubuntu:22.04",
        default_region="eu-west",
    )
    config = provider._parse_build_args([])
    assert config.gpu == "a100"
    assert config.image == "ubuntu:22.04"
    assert config.region == "eu-west"


def test_parse_build_args_explicit_args_override_config_defaults(temp_mng_ctx: MngContext) -> None:
    """Explicit build args should override config defaults."""
    provider = make_modal_provider_with_config_defaults(
        temp_mng_ctx,
        app_name="test-app",
        default_gpu="h100",
        default_image="python:3.11-slim",
        default_region="us-east",
    )

    # Override GPU
    config = provider._parse_build_args(["--gpu=a100"])
    assert config.gpu == "a100"
    assert config.image == "python:3.11-slim"
    assert config.region == "us-east"

    # Override image
    config = provider._parse_build_args(["--image=debian:bookworm"])
    assert config.gpu == "h100"
    assert config.image == "debian:bookworm"
    assert config.region == "us-east"

    # Override region
    config = provider._parse_build_args(["--region=eu-west"])
    assert config.gpu == "h100"
    assert config.image == "python:3.11-slim"
    assert config.region == "eu-west"

    # Override all
    config = provider._parse_build_args(["--gpu=t4", "--image=alpine:latest", "--region=ap-south"])
    assert config.gpu == "t4"
    assert config.image == "alpine:latest"
    assert config.region == "ap-south"


def test_modal_provider_config_user_id_defaults_to_none() -> None:
    """ModalProviderConfig user_id should default to None."""
    config = ModalProviderConfig()
    assert config.user_id is None


def test_modal_provider_config_user_id_can_be_set() -> None:
    """ModalProviderConfig user_id can be set to override profile user_id."""
    config = ModalProviderConfig(user_id=UserId("custom-user-id"))
    assert config.user_id == UserId("custom-user-id")


# =============================================================================
# Tests for _build_modal_secrets_from_env helper function
# =============================================================================


def test_build_modal_secrets_from_env_empty_list() -> None:
    """Empty list of env vars should return empty list of secrets."""
    result = _build_modal_secrets_from_env([])
    assert result == []


def test_build_modal_secrets_from_env_with_set_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Should create secrets from environment variables that are set."""
    monkeypatch.setenv("TEST_SECRET_1", "value1")
    monkeypatch.setenv("TEST_SECRET_2", "value2")

    result = _build_modal_secrets_from_env(["TEST_SECRET_1", "TEST_SECRET_2"])

    # All vars are combined into one Secret
    assert len(result) == 1


def test_build_modal_secrets_from_env_missing_var_raises_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Should raise MngError when an environment variable is not set."""
    # Ensure the variable is not set
    monkeypatch.delenv("NONEXISTENT_VAR", raising=False)

    with pytest.raises(MngError) as exc_info:
        _build_modal_secrets_from_env(["NONEXISTENT_VAR"])

    assert "Environment variable(s) not set for secrets" in str(exc_info.value)
    assert "NONEXISTENT_VAR" in str(exc_info.value)


def test_build_modal_secrets_from_env_multiple_missing_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Should report all missing environment variables in the error."""
    monkeypatch.delenv("MISSING_VAR_1", raising=False)
    monkeypatch.delenv("MISSING_VAR_2", raising=False)

    with pytest.raises(MngError) as exc_info:
        _build_modal_secrets_from_env(["MISSING_VAR_1", "MISSING_VAR_2"])

    error_message = str(exc_info.value)
    assert "MISSING_VAR_1" in error_message
    assert "MISSING_VAR_2" in error_message


def test_build_modal_secrets_from_env_partial_missing_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Should raise error listing only the missing vars when some are set."""
    monkeypatch.setenv("SET_VAR", "value")
    monkeypatch.delenv("MISSING_VAR", raising=False)

    with pytest.raises(MngError) as exc_info:
        _build_modal_secrets_from_env(["SET_VAR", "MISSING_VAR"])

    error_message = str(exc_info.value)
    assert "MISSING_VAR" in error_message
    assert "SET_VAR" not in error_message


# =============================================================================
# Tests for _create_shutdown_script helper method
# =============================================================================


def test_create_shutdown_script_generates_correct_content(
    modal_provider: ModalProviderInstance,
) -> None:
    """_create_shutdown_script should generate a script with correct content."""
    # Create a simple mock host that captures the written content
    written_content: dict[str, str] = {}
    written_modes: dict[str, str] = {}

    class MockHost:
        host_dir = Path("/mng")

        def write_text_file(self, path: Path, content: str, mode: str | None = None) -> None:
            written_content[str(path)] = content
            if mode:
                written_modes[str(path)] = mode

    mock_host = MockHost()

    # Create a mock sandbox with an object_id
    mock_sandbox = MagicMock()
    mock_sandbox.object_id = "sb-test-sandbox-123"

    # Call the method with a test URL
    host_id = HostId.generate()
    snapshot_url = "https://test--app-snapshot-and-shutdown.modal.run"

    modal_provider._create_shutdown_script(
        cast(Any, mock_host),
        mock_sandbox,
        host_id,
        snapshot_url,
    )

    # Verify the script was written to the correct path
    expected_path = "/mng/commands/shutdown.sh"
    assert expected_path in written_content

    # Verify the script content
    script = written_content[expected_path]
    assert "#!/bin/bash" in script
    assert snapshot_url in script
    assert "sb-test-sandbox-123" in script
    assert str(host_id) in script
    assert "curl" in script
    assert "Content-Type: application/json" in script

    # Verify the mode is executable
    assert written_modes[expected_path] == "755"


# =============================================================================
# Tests for persist_agent_data and remove_persisted_agent_data
# =============================================================================


def test_persist_agent_data_writes_to_volume(
    modal_provider: ModalProviderInstance,
) -> None:
    """persist_agent_data should write agent data as JSON to the volume."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    agent_data = {
        "id": str(agent_id),
        "name": "test-agent",
        "type": "test",
        "command": "echo hello",
    }

    # Track what was uploaded to the volume
    uploaded_files: dict[str, bytes] = {}

    class MockBatchUpload:
        def __init__(self) -> None:
            pass

        def __enter__(self) -> "MockBatchUpload":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def put_file(self, file_obj: Any, path: str) -> None:
            uploaded_files[path] = file_obj.read()

    mock_volume = cast(Any, modal_provider.modal_app.volume)
    mock_volume.batch_upload.return_value = MockBatchUpload()

    modal_provider.persist_agent_data(host_id, agent_data)

    # Verify the file was written to the correct path
    expected_path = f"/hosts/{host_id}/{agent_id}.json"
    assert expected_path in uploaded_files

    # Verify the content is valid JSON with expected fields
    uploaded_content = json.loads(uploaded_files[expected_path].decode("utf-8"))
    assert uploaded_content["id"] == str(agent_id)
    assert uploaded_content["name"] == "test-agent"
    assert uploaded_content["type"] == "test"
    assert uploaded_content["command"] == "echo hello"


def test_persist_agent_data_without_id_logs_warning_and_returns(
    modal_provider: ModalProviderInstance,
) -> None:
    """persist_agent_data should warn and return early if agent_data has no id field."""
    host_id = HostId.generate()
    agent_data: dict[str, object] = {
        "name": "test-agent",
        "type": "test",
    }

    mock_volume = cast(Any, modal_provider.modal_app.volume)

    # Should not raise, just return early
    modal_provider.persist_agent_data(host_id, agent_data)

    # Verify the volume was never accessed
    mock_volume.batch_upload.assert_not_called()


def test_remove_persisted_agent_data_removes_file(
    modal_provider: ModalProviderInstance,
) -> None:
    """remove_persisted_agent_data should remove the agent file from the volume."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()

    mock_volume = cast(Any, modal_provider.modal_app.volume)

    modal_provider.remove_persisted_agent_data(host_id, agent_id)

    expected_path = f"/hosts/{host_id}/{agent_id}.json"
    mock_volume.remove_file.assert_called_once_with(expected_path, recursive=False)


def test_remove_persisted_agent_data_handles_file_not_found(
    modal_provider: ModalProviderInstance,
) -> None:
    """remove_persisted_agent_data should silently handle FileNotFoundError."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()

    mock_volume = cast(Any, modal_provider.modal_app.volume)
    mock_volume.remove_file.side_effect = FileNotFoundError("File not found")

    # Should not raise
    modal_provider.remove_persisted_agent_data(host_id, agent_id)

    # Verify the method was called
    expected_path = f"/hosts/{host_id}/{agent_id}.json"
    mock_volume.remove_file.assert_called_once_with(expected_path, recursive=False)


# =============================================================================
# Tests for is_host_volume_created=False behavior
# =============================================================================


def _make_modal_provider_without_host_volume(
    mng_ctx: MngContext,
    app_name: str,
) -> ModalProviderInstance:
    """Create a ModalProviderInstance with is_host_volume_created=False."""
    mock_app = MagicMock()
    mock_app.app_id = "mock-app-id"
    mock_app.name = app_name

    mock_volume = MagicMock()
    output_buffer = StringIO()
    mock_environment_name = f"test-env-{app_name}"

    modal_app = ModalProviderApp.model_construct(
        app_name=app_name,
        environment_name=mock_environment_name,
        app=mock_app,
        volume=mock_volume,
        close_callback=MagicMock(),
        get_output_callback=output_buffer.getvalue,
    )

    config = ModalProviderConfig(
        app_name=app_name,
        host_dir=Path("/mng"),
        default_sandbox_timeout=300,
        default_cpu=0.5,
        default_memory=0.5,
        is_persistent=False,
        is_snapshotted_after_create=False,
        is_host_volume_created=False,
    )

    return ModalProviderInstance.model_construct(
        name=ProviderInstanceName("modal-test-no-vol"),
        host_dir=Path("/mng"),
        mng_ctx=mng_ctx,
        config=config,
        modal_app=modal_app,
    )


@pytest.fixture
def modal_provider_no_host_volume(temp_mng_ctx: MngContext, mng_test_id: str) -> ModalProviderInstance:
    """Create a ModalProviderInstance with is_host_volume_created=False."""
    app_name = f"{MODAL_TEST_APP_PREFIX}{mng_test_id}"
    return _make_modal_provider_without_host_volume(temp_mng_ctx, app_name)


def test_get_volume_for_host_returns_none_when_host_volume_disabled(
    modal_provider_no_host_volume: ModalProviderInstance,
) -> None:
    """get_volume_for_host should return None when is_host_volume_created=False."""
    host_id = HostId.generate()
    result = modal_provider_no_host_volume.get_volume_for_host(host_id)
    assert result is None


def test_shutdown_script_omits_volume_sync_when_host_volume_disabled(
    modal_provider_no_host_volume: ModalProviderInstance,
) -> None:
    """Shutdown script should not include volume sync when is_host_volume_created=False."""
    written_content: dict[str, str] = {}

    class MockHost:
        host_dir = Path("/mng")

        def write_text_file(self, path: Path, content: str, mode: str | None = None) -> None:
            written_content[str(path)] = content

    mock_sandbox = MagicMock()
    mock_sandbox.object_id = "sb-test-9182736"

    host_id = HostId.generate()
    modal_provider_no_host_volume._create_shutdown_script(
        cast(Any, MockHost()),
        mock_sandbox,
        host_id,
        "https://test--snapshot.modal.run",
    )

    script = written_content["/mng/commands/shutdown.sh"]
    assert "sync /host_volume" not in script


def test_shutdown_script_includes_volume_sync_when_host_volume_enabled(
    modal_provider: ModalProviderInstance,
) -> None:
    """Shutdown script should include volume sync when is_host_volume_created=True."""
    written_content: dict[str, str] = {}

    class MockHost:
        host_dir = Path("/mng")

        def write_text_file(self, path: Path, content: str, mode: str | None = None) -> None:
            written_content[str(path)] = content

    mock_sandbox = MagicMock()
    mock_sandbox.object_id = "sb-test-4567890"

    host_id = HostId.generate()
    modal_provider._create_shutdown_script(
        cast(Any, MockHost()),
        mock_sandbox,
        host_id,
        "https://test--snapshot.modal.run",
    )

    script = written_content["/mng/commands/shutdown.sh"]
    assert "sync /host_volume" in script


def test_is_host_volume_created_defaults_to_true() -> None:
    """ModalProviderConfig.is_host_volume_created should default to True."""
    config = ModalProviderConfig()
    assert config.is_host_volume_created is True


# =============================================================================
# Tests for _list_all_host_and_agent_records
# =============================================================================


def test_list_all_host_and_agent_records_returns_empty_when_volume_empty(
    modal_provider: ModalProviderInstance,
) -> None:
    """_list_all_host_and_agent_records returns empty results when volume has no entries."""
    mock_volume = cast(Any, modal_provider.modal_app.volume)
    mock_volume.listdir.return_value = []

    host_records, agent_data = modal_provider._list_all_host_and_agent_records(
        modal_provider.mng_ctx.concurrency_group
    )

    assert host_records == []
    assert agent_data == {}
    mock_volume.listdir.assert_called_once_with("/hosts/")


def test_list_all_host_and_agent_records_returns_host_records_and_agent_data(
    modal_provider: ModalProviderInstance,
) -> None:
    """_list_all_host_and_agent_records reads host records and agent data for .json entries."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    host_record = _make_host_record(host_id)
    agent_data_list = [{"id": str(agent_id), "name": "test-agent", "type": "claude"}]

    mock_entry = MagicMock()
    mock_entry.path = f"hosts/{host_id}.json"
    mock_volume = cast(Any, modal_provider.modal_app.volume)
    mock_volume.listdir.return_value = [mock_entry]

    with (
        patch.object(modal_provider, "_read_host_record", return_value=host_record),
        patch.object(ModalProviderInstance, "list_persisted_agent_data_for_host", return_value=agent_data_list),
    ):
        host_records, agent_data = modal_provider._list_all_host_and_agent_records(
            modal_provider.mng_ctx.concurrency_group
        )

    assert len(host_records) == 1
    assert host_records[0].certified_host_data.host_id == str(host_id)
    assert host_id in agent_data
    assert len(agent_data[host_id]) == 1
    assert agent_data[host_id][0]["id"] == str(agent_id)


def test_list_all_host_and_agent_records_skips_non_json_files(
    modal_provider: ModalProviderInstance,
) -> None:
    """_list_all_host_and_agent_records only processes .json files."""
    mock_entry_json = MagicMock()
    mock_entry_json.path = f"hosts/{HostId.generate()}.json"
    mock_entry_dir = MagicMock()
    mock_entry_dir.path = "hosts/some-directory"

    mock_volume = cast(Any, modal_provider.modal_app.volume)
    mock_volume.listdir.return_value = [mock_entry_json, mock_entry_dir]

    with (
        patch.object(modal_provider, "_read_host_record", return_value=None) as mock_read,
        patch.object(ModalProviderInstance, "list_persisted_agent_data_for_host", return_value=[]) as mock_list_agent,
    ):
        modal_provider._list_all_host_and_agent_records(modal_provider.mng_ctx.concurrency_group)
        assert mock_read.call_count == 1
        assert mock_list_agent.call_count == 1


def test_list_all_host_and_agent_records_without_agents(
    modal_provider: ModalProviderInstance,
) -> None:
    """_list_all_host_and_agent_records with is_including_agents=False skips agent data."""
    host_id = HostId.generate()
    host_record = _make_host_record(host_id)

    mock_entry = MagicMock()
    mock_entry.path = f"hosts/{host_id}.json"
    mock_volume = cast(Any, modal_provider.modal_app.volume)
    mock_volume.listdir.return_value = [mock_entry]

    with (
        patch.object(modal_provider, "_read_host_record", return_value=host_record),
        patch.object(ModalProviderInstance, "list_persisted_agent_data_for_host") as mock_list_agent,
    ):
        host_records, agent_data = modal_provider._list_all_host_and_agent_records(
            modal_provider.mng_ctx.concurrency_group, is_including_agents=False
        )

    assert len(host_records) == 1
    assert agent_data == {}
    mock_list_agent.assert_not_called()


def test_list_all_host_and_agent_records_skips_none_host_records(
    modal_provider: ModalProviderInstance,
) -> None:
    """_list_all_host_and_agent_records filters out None results from _read_host_record."""
    host_id = HostId.generate()

    mock_entry = MagicMock()
    mock_entry.path = f"hosts/{host_id}.json"
    mock_volume = cast(Any, modal_provider.modal_app.volume)
    mock_volume.listdir.return_value = [mock_entry]

    with (
        patch.object(modal_provider, "_read_host_record", return_value=None),
        patch.object(ModalProviderInstance, "list_persisted_agent_data_for_host", return_value=[]),
    ):
        host_records, agent_data = modal_provider._list_all_host_and_agent_records(
            modal_provider.mng_ctx.concurrency_group
        )

    assert host_records == []


# =============================================================================
# Tests for _list_running_host_ids
# =============================================================================


def test_list_running_host_ids_returns_empty_when_no_sandboxes(
    modal_provider: ModalProviderInstance,
) -> None:
    """_list_running_host_ids returns empty set when no sandboxes exist."""
    with patch.object(modal, "Sandbox") as mock_sandbox_cls:
        mock_sandbox_cls.list.return_value = []
        result = modal_provider._list_running_host_ids(modal_provider.mng_ctx.concurrency_group)

    assert result == set()


def test_list_running_host_ids_fetches_tags_in_parallel(
    modal_provider: ModalProviderInstance,
) -> None:
    """_list_running_host_ids fetches tags for all sandboxes and extracts host IDs."""
    host_id_1 = HostId.generate()
    host_id_2 = HostId.generate()

    sandbox_1 = MagicMock()
    sandbox_1.get_tags.return_value = {TAG_HOST_ID: str(host_id_1), TAG_HOST_NAME: "host-1"}
    sandbox_2 = MagicMock()
    sandbox_2.get_tags.return_value = {TAG_HOST_ID: str(host_id_2), TAG_HOST_NAME: "host-2"}

    with patch.object(modal, "Sandbox") as mock_sandbox_cls:
        mock_sandbox_cls.list.return_value = [sandbox_1, sandbox_2]
        result = modal_provider._list_running_host_ids(modal_provider.mng_ctx.concurrency_group)

    assert result == {host_id_1, host_id_2}


def test_list_running_host_ids_skips_sandboxes_without_host_id_tag(
    modal_provider: ModalProviderInstance,
) -> None:
    """_list_running_host_ids skips sandboxes that don't have the host ID tag."""
    host_id = HostId.generate()

    sandbox_with_tag = MagicMock()
    sandbox_with_tag.get_tags.return_value = {TAG_HOST_ID: str(host_id)}
    sandbox_without_tag = MagicMock()
    sandbox_without_tag.get_tags.return_value = {"some_other_tag": "value"}

    with patch.object(modal, "Sandbox") as mock_sandbox_cls:
        mock_sandbox_cls.list.return_value = [sandbox_with_tag, sandbox_without_tag]
        result = modal_provider._list_running_host_ids(modal_provider.mng_ctx.concurrency_group)

    assert result == {host_id}


# =============================================================================
# Tests for discover_hosts_and_agents (optimized modal implementation)
# =============================================================================


def test_discover_hosts_and_agents_returns_agents_from_volume_data(
    modal_provider: ModalProviderInstance,
) -> None:
    """discover_hosts_and_agents builds DiscoveredHost->DiscoveredAgent map from volume data."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    snapshot = _make_snapshot_record("initial")
    host_record = _make_host_record(host_id, host_name="my-host", snapshots=[snapshot])
    agent_data = [{"id": str(agent_id), "name": "test-agent", "type": "claude"}]

    with (
        patch.object(modal_provider, "_list_running_host_ids", return_value=set()),
        patch.object(
            modal_provider,
            "_list_all_host_and_agent_records",
            return_value=([host_record], {host_id: agent_data}),
        ),
    ):
        result = modal_provider.discover_hosts_and_agents(cg=modal_provider.mng_ctx.concurrency_group)

    assert len(result) == 1
    host_ref = next(iter(result.keys()))
    assert host_ref.host_id == host_id
    assert host_ref.host_name == "my-host"
    assert host_ref.provider_name == modal_provider.name

    agent_refs = result[host_ref]
    assert len(agent_refs) == 1
    assert agent_refs[0].agent_id == agent_id


def test_discover_hosts_and_agents_excludes_destroyed_hosts_by_default(
    modal_provider: ModalProviderInstance,
) -> None:
    """discover_hosts_and_agents excludes destroyed hosts (no sandbox, no snapshots) by default."""
    host_id = HostId.generate()
    host_record = _make_host_record(host_id, snapshots=[])

    with (
        patch.object(modal_provider, "_list_running_host_ids", return_value=set()),
        patch.object(modal_provider, "_list_all_host_and_agent_records", return_value=([host_record], {})),
    ):
        result = modal_provider.discover_hosts_and_agents(
            cg=modal_provider.mng_ctx.concurrency_group, include_destroyed=False
        )

    assert len(result) == 0


def test_discover_hosts_and_agents_includes_destroyed_hosts_when_requested(
    modal_provider: ModalProviderInstance,
) -> None:
    """discover_hosts_and_agents includes destroyed hosts when include_destroyed=True."""
    host_id = HostId.generate()
    host_record = _make_host_record(host_id, snapshots=[])

    with (
        patch.object(modal_provider, "_list_running_host_ids", return_value=set()),
        patch.object(modal_provider, "_list_all_host_and_agent_records", return_value=([host_record], {})),
    ):
        result = modal_provider.discover_hosts_and_agents(
            cg=modal_provider.mng_ctx.concurrency_group, include_destroyed=True
        )

    assert len(result) == 1


def test_discover_hosts_and_agents_includes_running_hosts_from_host_records(
    modal_provider: ModalProviderInstance,
) -> None:
    """discover_hosts_and_agents includes running hosts (sandbox exists + host record exists)."""
    host_id = HostId.generate()
    host_record = _make_host_record(host_id, host_name="running-host", snapshots=[])

    with (
        patch.object(modal_provider, "_list_running_host_ids", return_value={host_id}),
        patch.object(modal_provider, "_list_all_host_and_agent_records", return_value=([host_record], {})),
    ):
        result = modal_provider.discover_hosts_and_agents(cg=modal_provider.mng_ctx.concurrency_group)

    # Running host (sandbox exists) should be included even without snapshots
    assert len(result) == 1
    host_ref = next(iter(result.keys()))
    assert host_ref.host_id == host_id


def test_discover_hosts_and_agents_ignores_running_sandbox_without_host_record(
    modal_provider: ModalProviderInstance,
) -> None:
    """discover_hosts_and_agents does not create entries for sandboxes that have no host record."""
    orphan_host_id = HostId.generate()

    with (
        patch.object(modal_provider, "_list_running_host_ids", return_value={orphan_host_id}),
        patch.object(modal_provider, "_list_all_host_and_agent_records", return_value=([], {})),
    ):
        result = modal_provider.discover_hosts_and_agents(cg=modal_provider.mng_ctx.concurrency_group)

    assert len(result) == 0


# =============================================================================
# Docker Build Args Tests
# =============================================================================


def test_parse_build_args_docker_build_arg(modal_provider: ModalProviderInstance) -> None:
    """Should parse --docker-build-arg arguments."""
    config = modal_provider._parse_build_args(["--docker-build-arg=CLAUDE_CODE_VERSION=2.1.50"])
    assert config.docker_build_args == ("CLAUDE_CODE_VERSION=2.1.50",)


def test_parse_build_args_multiple_docker_build_args(modal_provider: ModalProviderInstance) -> None:
    """Should parse multiple --docker-build-arg arguments."""
    config = modal_provider._parse_build_args(
        [
            "docker-build-arg=CLAUDE_CODE_VERSION=2.1.50",
            "docker-build-arg=OTHER_ARG=value",
        ]
    )
    assert config.docker_build_args == ("CLAUDE_CODE_VERSION=2.1.50", "OTHER_ARG=value")


def test_parse_build_args_docker_build_arg_default_empty(modal_provider: ModalProviderInstance) -> None:
    """docker_build_args should default to empty tuple."""
    config = modal_provider._parse_build_args([])
    assert config.docker_build_args == ()


def test_substitute_dockerfile_build_args_replaces_default() -> None:
    """_substitute_dockerfile_build_args should replace ARG defaults."""
    dockerfile = 'FROM python:3.11-slim\nARG CLAUDE_CODE_VERSION=""\nRUN echo $CLAUDE_CODE_VERSION'
    result = _substitute_dockerfile_build_args(dockerfile, ("CLAUDE_CODE_VERSION=2.1.50",))
    assert 'ARG CLAUDE_CODE_VERSION="2.1.50"' in result
    assert 'ARG CLAUDE_CODE_VERSION=""' not in result


def test_substitute_dockerfile_build_args_replaces_non_empty_default() -> None:
    """_substitute_dockerfile_build_args should replace non-empty ARG defaults."""
    dockerfile = 'FROM python:3.11\nARG MY_VERSION="1.0.0"\n'
    result = _substitute_dockerfile_build_args(dockerfile, ("MY_VERSION=2.0.0",))
    assert 'ARG MY_VERSION="2.0.0"' in result


def test_substitute_dockerfile_build_args_raises_for_missing_arg() -> None:
    """_substitute_dockerfile_build_args should raise if ARG is not found."""
    dockerfile = "FROM python:3.11-slim\nRUN echo hello\n"
    with pytest.raises(MngError, match="not found as an ARG instruction"):
        _substitute_dockerfile_build_args(dockerfile, ("NONEXISTENT_ARG=value",))


def test_substitute_dockerfile_build_args_raises_for_bad_format() -> None:
    """_substitute_dockerfile_build_args should raise for non KEY=VALUE format."""
    dockerfile = 'FROM python:3.11-slim\nARG FOO=""\n'
    with pytest.raises(MngError, match="KEY=VALUE format"):
        _substitute_dockerfile_build_args(dockerfile, ("no-equals-sign",))


# =============================================================================
# Tests for check_host_name_is_unique
# =============================================================================


def test_check_host_name_is_unique_passes_when_no_existing_hosts() -> None:
    """check_host_name_is_unique should not raise when there are no existing hosts."""
    check_host_name_is_unique(HostName("new-host"), host_records=[], running_host_ids=set())


def test_check_host_name_is_unique_passes_when_name_is_different() -> None:
    """check_host_name_is_unique should not raise when the name is different from existing hosts."""
    host_id = HostId.generate()
    existing_record = _make_host_record(host_id, host_name="existing-host", snapshots=[_make_snapshot_record()])

    check_host_name_is_unique(HostName("different-host"), host_records=[existing_record], running_host_ids=set())


def test_check_host_name_is_unique_raises_when_name_already_exists_on_running_host() -> None:
    """check_host_name_is_unique should raise HostNameConflictError when a running host has the same name."""
    host_id = HostId.generate()
    existing_record = _make_host_record(host_id, host_name="taken-name")

    with pytest.raises(HostNameConflictError) as exc_info:
        check_host_name_is_unique(HostName("taken-name"), host_records=[existing_record], running_host_ids={host_id})
    assert "taken-name" in str(exc_info.value)


def test_check_host_name_is_unique_raises_when_name_exists_on_stopped_host_with_snapshots() -> None:
    """check_host_name_is_unique should raise when a stopped host with snapshots has the same name."""
    host_id = HostId.generate()
    existing_record = _make_host_record(host_id, host_name="taken-name", snapshots=[_make_snapshot_record()])

    with pytest.raises(HostNameConflictError):
        check_host_name_is_unique(HostName("taken-name"), host_records=[existing_record], running_host_ids=set())


def test_check_host_name_is_unique_allows_reuse_of_destroyed_host_name() -> None:
    """check_host_name_is_unique should allow reusing a name from a destroyed host."""
    host_id = HostId.generate()
    # A destroyed host: no snapshots, no failure_reason, not running
    destroyed_record = _make_host_record(host_id, host_name="reusable-name", snapshots=[])

    # Should not raise
    check_host_name_is_unique(HostName("reusable-name"), host_records=[destroyed_record], running_host_ids=set())


def test_check_host_name_is_unique_raises_when_name_matches_any_non_destroyed() -> None:
    """check_host_name_is_unique should raise if the name matches any non-destroyed host."""
    host_records = [
        _make_host_record(HostId.generate(), host_name="host-alpha", snapshots=[_make_snapshot_record()]),
        _make_host_record(HostId.generate(), host_name="host-beta", snapshots=[_make_snapshot_record()]),
        _make_host_record(HostId.generate(), host_name="host-gamma", snapshots=[_make_snapshot_record()]),
    ]

    with pytest.raises(HostNameConflictError):
        check_host_name_is_unique(HostName("host-beta"), host_records=host_records, running_host_ids=set())


def test_check_host_name_is_unique_raises_when_name_exists_on_failed_host() -> None:
    """check_host_name_is_unique should raise for a failed host (not running, no snapshots, but has failure_reason)."""
    host_id = HostId.generate()
    failed_record = _make_host_record(host_id, host_name="failed-host", failure_reason="Build failed")

    with pytest.raises(HostNameConflictError):
        check_host_name_is_unique(HostName("failed-host"), host_records=[failed_record], running_host_ids=set())
