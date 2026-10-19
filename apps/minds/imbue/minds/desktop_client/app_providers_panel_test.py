"""The providers panel's connection rows: how a connection's availability and discovery's verdict combine."""

from imbue.minds.desktop_client.app import _connection_entry
from imbue.minds.desktop_client.connections.data_types import ConnectionAvailability
from imbue.minds.desktop_client.connections.data_types import ConnectionView
from imbue.minds.desktop_client.ui_models import ConnectionPanelStatus
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.errors import ProviderCredentialSourceMissingError
from imbue.mngr.primitives import ProviderInstanceName

_INSTANCE = "aws_" + "a" * 32


def _view(availability: ConnectionAvailability, credential_source: str) -> ConnectionView:
    return ConnectionView(
        connection_id="conn-" + "a" * 32,
        instance_name=_INSTANCE,
        backend="aws",
        alias="work aws",
        identifier="profile work",
        placement={"default_region": "us-west-2", "aws_profile": "work"},
        owner_user_id="local",
        availability=availability,
        credential_source=credential_source,
    )


def test_a_connection_whose_login_is_missing_here_reads_profile_missing_with_the_fix() -> None:
    error = DiscoveryError(
        type_name=ProviderCredentialSourceMissingError.__name__,
        message="AWS profile 'work' is not configured on this machine",
        provider_name=ProviderInstanceName(_INSTANCE),
        remediation="run `aws configure --profile work`",
    )

    entry = _connection_entry(_view(ConnectionAvailability.AVAILABLE, "local_profile"), {}, {_INSTANCE: error}, 0)

    assert entry.status is ConnectionPanelStatus.PROFILE_MISSING
    assert entry.remediation == "run `aws configure --profile work`"
    assert entry.error_message == "AWS profile 'work' is not configured on this machine"
    assert entry.credential_source == "local_profile"


def test_any_other_discovery_error_still_reads_as_an_error_and_carries_the_providers_fix() -> None:
    error = DiscoveryError(
        type_name="ProviderNotAuthorizedError",
        message="assume-role refused",
        provider_name=ProviderInstanceName(_INSTANCE),
        remediation="check the profile with `aws sts get-caller-identity --profile work`",
    )

    entry = _connection_entry(_view(ConnectionAvailability.AVAILABLE, "local_profile"), {}, {_INSTANCE: error}, 0)

    assert entry.status is ConnectionPanelStatus.ERROR
    assert entry.remediation == "check the profile with `aws sts get-caller-identity --profile work`"


def test_a_healthy_connection_reads_ok_with_no_remediation() -> None:
    entry = _connection_entry(_view(ConnectionAvailability.AVAILABLE, "pasted"), {_INSTANCE: "aws"}, {}, 2)

    assert entry.status is ConnectionPanelStatus.OK
    assert entry.remediation is None and entry.credential_source == "pasted" and entry.workspace_count == 2
