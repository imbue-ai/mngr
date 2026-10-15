import pytest

from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import ConnectionId
from imbue.minds.desktop_client.connections.primitives import MODAL_USER_ID_RE
from imbue.minds.desktop_client.connections.primitives import azure_resource_group_for_connection
from imbue.minds.desktop_client.connections.primitives import backend_for_connection_instance_name
from imbue.minds.desktop_client.connections.primitives import connection_id_hex
from imbue.minds.desktop_client.connections.primitives import imbue_cloud_provider_name_for_user
from imbue.minds.desktop_client.connections.primitives import is_connection_instance_name
from imbue.minds.desktop_client.connections.primitives import provider_instance_name_for_connection
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_modal.backend import MODAL_NAME_MAX_LENGTH
from imbue.mngr_modal.backend import STATE_VOLUME_SUFFIX
from imbue.mngr_modal.backend import truncate_modal_name

_USER_ID = "3f2a9c1e-5b7d-4a8e-9c0f-1d2e3f4a5b6c"


def test_account_instance_name_carries_the_full_user_id_and_is_a_valid_provider_name() -> None:
    name = imbue_cloud_provider_name_for_user(_USER_ID)
    assert name == f"imbue_cloud_{_USER_ID}"
    assert ProviderInstanceName(name) == name


def test_connection_instance_name_is_backend_and_hex() -> None:
    connection_id = ConnectionId.generate()
    name = provider_instance_name_for_connection(ConnectionBackend.AWS, connection_id)
    assert name == f"aws_{connection_id_hex(connection_id)}"
    assert len(connection_id_hex(connection_id)) == 32
    assert ProviderInstanceName(name) == name
    assert is_connection_instance_name(name)
    assert backend_for_connection_instance_name(name) is ConnectionBackend.AWS


@pytest.mark.parametrize(
    "name",
    ["aws", "aws-us-east-1", "byok-aws-mine", f"imbue_cloud_{_USER_ID}", "vultr_" + "0" * 32, "modal_abc"],
)
def test_other_provider_names_are_not_connection_instances(name: str) -> None:
    assert not is_connection_instance_name(name)
    assert backend_for_connection_instance_name(name) is None


def test_azure_resource_group_is_minted_from_the_connection_id_and_region() -> None:
    connection_id = ConnectionId.generate()
    assert (
        azure_resource_group_for_connection(connection_id, "eastus2")
        == f"mngr-{connection_id_hex(connection_id)}-eastus2"
    )


@pytest.mark.parametrize("root_name", ["minds", "minds-staging", "minds-dev-josh-1"])
def test_modal_names_derived_from_a_connection_are_never_truncated(root_name: str) -> None:
    """The Modal block's ``user_id`` is the connection id hex, so every device derives the same environment.

    ``ModalProviderBackend._derive_modal_names`` truncates an environment name past
    MODAL_NAME_MAX_LENGTH and an app name past that minus the state-volume suffix;
    a truncated name would silently split one connection into two environments.
    The minds prefix is ``<root name>-``; this pins that neither name truncates for
    production, staging, or a typical dev env.
    """
    prefix = f"{root_name}-"
    connection_id = ConnectionId.generate()
    environment_name = f"{prefix}{connection_id_hex(connection_id)}"
    app_name = f"{prefix}{provider_instance_name_for_connection(ConnectionBackend.MODAL, connection_id)}"
    assert truncate_modal_name(environment_name, max_length=MODAL_NAME_MAX_LENGTH) == environment_name
    assert truncate_modal_name(app_name, max_length=MODAL_NAME_MAX_LENGTH - len(STATE_VOLUME_SUFFIX)) == app_name


def test_a_named_modal_namespace_of_the_longest_allowed_shape_is_never_truncated_under_the_staging_prefix() -> None:
    """The evals harness names a trial's environment ``minds-staging-<48-character user id>``.

    MODAL_USER_ID_RE allows 48 characters, the harness's own USER_ID_MAX_LENGTH;
    under the 14-character staging prefix the environment name is 62 characters,
    inside MODAL_NAME_MAX_LENGTH. The dev root names (``minds-dev-josh-1``) exceed
    the budget with this shape, which the harness's own budget check refuses up
    front, so this case stays outside the root-name parametrization above.
    """
    modal_user_id = "evals-ci-" + "x" * 39
    assert len(modal_user_id) == 48
    assert MODAL_USER_ID_RE.match(modal_user_id) is not None
    environment_name = f"minds-staging-{modal_user_id}"
    assert truncate_modal_name(environment_name, max_length=MODAL_NAME_MAX_LENGTH) == environment_name
    assert MODAL_USER_ID_RE.match(modal_user_id + "x") is None
