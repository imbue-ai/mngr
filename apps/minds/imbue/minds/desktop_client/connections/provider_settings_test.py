import json
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest
import tomlkit
from inline_snapshot import snapshot
from pydantic import SecretStr

from imbue.minds.bootstrap import MindsRoot
from imbue.minds.desktop_client.conftest import add_modal_connection_for_test
from imbue.minds.desktop_client.conftest import make_connection_store_for_test
from imbue.minds.desktop_client.conftest import make_provider_materializer_for_test
from imbue.minds.desktop_client.conftest import unlock_account_with_master_password_for_test
from imbue.minds.desktop_client.connections.data_types import ConnectionCredentialSource
from imbue.minds.desktop_client.connections.data_types import ConnectionCredentials
from imbue.minds.desktop_client.connections.data_types import ConnectionMetadata
from imbue.minds.desktop_client.connections.data_types import ConnectionReplicaRecord
from imbue.minds.desktop_client.connections.data_types import ProviderMaterializationOutcome
from imbue.minds.desktop_client.connections.data_types import SignedInAccount
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import ConnectionId
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.connections.primitives import imbue_cloud_provider_name_for_user
from imbue.minds.desktop_client.connections.primitives import provider_instance_name_for_connection
from imbue.minds.desktop_client.connections.provider_settings import ProviderSettingsMaterializer
from imbue.minds.desktop_client.connections.provider_settings import connection_provider_block
from imbue.minds.desktop_client.connections.provider_settings import desired_provider_blocks
from imbue.minds.desktop_client.connections.provider_settings import imbue_cloud_accounts_index_path
from imbue.minds.desktop_client.connections.provider_settings import is_managed_provider_name
from imbue.minds.desktop_client.connections.provider_settings import is_provider_polled_here
from imbue.minds.desktop_client.connections.provider_settings import legacy_imbue_cloud_provider_name_for_account
from imbue.minds.desktop_client.connections.provider_settings import migrate_imbue_cloud_state_dirs
from imbue.minds.desktop_client.connections.provider_settings import read_signed_in_accounts_from_index
from imbue.minds.desktop_client.connections.provider_settings import reconcile_provider_settings
from imbue.minds.desktop_client.dek_store import delete_dek
from imbue.minds.errors import AccountsIndexUnreadableError
from imbue.minds.testing import stub_mngr_host_dir
from imbue.minds.testing import write_imbue_cloud_accounts_index

_ROOT_NAME = "minds-dev-tname"
_ROOT = MindsRoot(_ROOT_NAME)
_CONNECTOR_URL = "https://test--rsc-api.modal.run"
_USER_ID = "3f2a9c1e-5b7d-4a8e-9c0f-1d2e3f4a5b6c"
_EMAIL = "alice@example.com"
_ACCOUNT = SignedInAccount(user_id=_USER_ID, email=_EMAIL)
_CONNECTION_ID = ConnectionId("conn-0123456789abcdef0123456789abcdef")


def _record(backend: str, placement: dict[str, object], credential_source: str = "pasted") -> ConnectionReplicaRecord:
    metadata = ConnectionMetadata(
        backend=backend,
        alias="my " + backend,
        identifier="hint",
        placement=placement,
        credential_source=credential_source,
    )
    return ConnectionReplicaRecord(doc_id=str(_CONNECTION_ID), metadata=metadata.model_dump(mode="json"))


def _blocks_toml(blocks: dict[str, dict[str, object]]) -> str:
    return tomlkit.dumps({"providers": blocks})


def _managed_names(providers: dict[str, object]) -> list[str]:
    """The materializer-owned blocks in a parsed ``[providers]`` table (the suppression blocks aside)."""
    return [name for name in providers if is_managed_provider_name(name)]


def test_account_instance_block_snapshot() -> None:
    blocks = desired_provider_blocks([_ACCOUNT], {}, {}, _CONNECTOR_URL)
    assert _blocks_toml(blocks) == snapshot("""\
[providers.imbue_cloud_3f2a9c1e-5b7d-4a8e-9c0f-1d2e3f4a5b6c]
backend = "imbue_cloud"
account = "alice@example.com"
connector_url = "https://test--rsc-api.modal.run"
docker_runtime = "runsc"
install_gvisor_runtime = true
default_start_args = ["--workdir=/", "--security-opt=no-new-privileges"]
host_dir = "/home/user/.mngr"
volume_home_path = "/home/user"
host_log_dir = "/var/log/mngr"
""")


def test_aws_connection_block_snapshot() -> None:
    record = _record("aws", {"default_region": "us-west-2", "default_instance_type": "t3.large"})
    credentials = ConnectionCredentials(
        aws_access_key_id="AKIA1234EXAMPLE9", aws_secret_access_key=SecretStr("s3cr3t")
    )
    blocks = desired_provider_blocks([], {_USER_ID: [record]}, {record.doc_id: credentials}, _CONNECTOR_URL)
    assert _blocks_toml(blocks) == snapshot("""\
[providers.aws_0123456789abcdef0123456789abcdef]
backend = "aws"
default_region = "us-west-2"
default_instance_type = "t3.large"
install_gvisor_runtime = true
docker_runtime = "runsc"
host_dir = "/home/user/.mngr"
volume_home_path = "/home/user"
host_log_dir = "/var/log/mngr"
aws_access_key_id = "AKIA1234EXAMPLE9"
aws_secret_access_key = "s3cr3t"
""")


def test_gcp_connection_block_snapshot() -> None:
    record = _record("gcp", {"default_zone": "us-west1-a", "default_machine_type": "e2-standard-2"})
    key_json = json.dumps({"type": "service_account", "client_email": "svc@proj.iam.gserviceaccount.com"})
    credentials = ConnectionCredentials(service_account_key_json=SecretStr(key_json))
    blocks = desired_provider_blocks([], {_USER_ID: [record]}, {record.doc_id: credentials}, _CONNECTOR_URL)
    assert _blocks_toml(blocks) == snapshot("""\
[providers.gcp_0123456789abcdef0123456789abcdef]
backend = "gcp"
default_zone = "us-west1-a"
default_machine_type = "e2-standard-2"
install_gvisor_runtime = true
docker_runtime = "runsc"
host_dir = "/home/user/.mngr"
volume_home_path = "/home/user"
host_log_dir = "/var/log/mngr"
service_account_key_json = "{\\"type\\": \\"service_account\\", \\"client_email\\": \\"svc@proj.iam.gserviceaccount.com\\"}"
""")


def test_azure_connection_block_snapshot() -> None:
    record = _record(
        "azure",
        {"default_region": "eastus2", "default_vm_size": "Standard_B2ms", "resource_group": "mngr-0123-eastus2"},
    )
    credentials = ConnectionCredentials(
        subscription_id="sub", tenant_id="tenant", client_id="client", client_secret=SecretStr("shh")
    )
    blocks = desired_provider_blocks([], {_USER_ID: [record]}, {record.doc_id: credentials}, _CONNECTOR_URL)
    assert _blocks_toml(blocks) == snapshot("""\
[providers.azure_0123456789abcdef0123456789abcdef]
backend = "azure"
default_region = "eastus2"
default_vm_size = "Standard_B2ms"
resource_group = "mngr-0123-eastus2"
install_gvisor_runtime = true
docker_runtime = "runsc"
host_dir = "/home/user/.mngr"
volume_home_path = "/home/user"
host_log_dir = "/var/log/mngr"
subscription_id = "sub"
tenant_id = "tenant"
client_id = "client"
client_secret = "shh"
""")


def test_modal_connection_block_snapshot_pins_the_connection_hex_as_user_id() -> None:
    record = _record("modal", {})
    credentials = ConnectionCredentials(token_id="ak-123", token_secret=SecretStr("as-456"))
    blocks = desired_provider_blocks(
        [], {LOCAL_PROFILE_USER_ID: [record]}, {record.doc_id: credentials}, _CONNECTOR_URL
    )
    assert _blocks_toml(blocks) == snapshot("""\
[providers.modal_0123456789abcdef0123456789abcdef]
backend = "modal"
mode = "DIRECT"
is_persistent = true
user_id = "0123456789abcdef0123456789abcdef"
default_cpu = 2.0
default_memory = 4.0
default_sandbox_timeout = 86310
default_idle_timeout = 86400
token_id = "ak-123"
token_secret = "as-456"
""")


def test_modal_connection_block_takes_the_named_namespace_over_the_connection_hex() -> None:
    record = _record("modal", {"user_id": "evals-ci-20261015-abcdef"})
    credentials = ConnectionCredentials(token_id="ak-123", token_secret=SecretStr("as-456"))

    blocks = desired_provider_blocks(
        [], {LOCAL_PROFILE_USER_ID: [record]}, {record.doc_id: credentials}, _CONNECTOR_URL
    )

    (block,) = blocks.values()
    assert block["user_id"] == "evals-ci-20261015-abcdef"


def test_connections_without_available_credentials_or_with_unknown_backends_are_skipped() -> None:
    aws = _record("aws", {"default_region": "us-west-2", "default_instance_type": "t3.large"})
    unknown = ConnectionReplicaRecord(
        doc_id=str(ConnectionId.generate()), metadata={"backend": "digitalocean", "alias": "x"}
    )
    too_new = ConnectionReplicaRecord(
        doc_id=str(ConnectionId.generate()), metadata={"backend": "aws", "alias": "y", "connection_format": 2}
    )
    incomplete = ConnectionReplicaRecord(
        doc_id=str(ConnectionId.generate()),
        metadata={"backend": "aws", "alias": "z", "placement": {"default_region": "us-west-2"}},
    )
    credentials = ConnectionCredentials(aws_access_key_id="AKIA", aws_secret_access_key=SecretStr("s"))
    blocks = desired_provider_blocks(
        [_ACCOUNT],
        {_USER_ID: [aws, unknown, too_new, incomplete]},
        {unknown.doc_id: credentials, too_new.doc_id: credentials, incomplete.doc_id: credentials},
        _CONNECTOR_URL,
    )
    # Only the account instance: aws lacks credentials, the others are unusable.
    assert list(blocks) == [imbue_cloud_provider_name_for_user(_USER_ID)]


def test_aws_local_profile_connection_block_names_the_profile_and_carries_no_keys() -> None:
    record = _record(
        "aws",
        {"default_region": "us-west-2", "default_instance_type": "t3.large", "aws_profile": "work"},
        "local_profile",
    )
    blocks = desired_provider_blocks([], {_USER_ID: [record]}, {}, _CONNECTOR_URL)
    assert _blocks_toml(blocks) == snapshot("""\
[providers.aws_0123456789abcdef0123456789abcdef]
backend = "aws"
default_region = "us-west-2"
default_instance_type = "t3.large"
install_gvisor_runtime = true
docker_runtime = "runsc"
host_dir = "/home/user/.mngr"
volume_home_path = "/home/user"
host_log_dir = "/var/log/mngr"
aws_profile = "work"
""")


def test_azure_local_profile_connection_block_pins_the_az_login_to_its_tenant_and_subscription() -> None:
    record = _record(
        "azure",
        {
            "default_region": "eastus",
            "default_vm_size": "Standard_B2s",
            "resource_group": "mngr-0123456789abcdef0123456789abcdef-eastus",
            "tenant_id": "tenant-1",
            "subscription_id": "sub-1",
        },
        "local_profile",
    )
    blocks = desired_provider_blocks([], {_USER_ID: [record]}, {}, _CONNECTOR_URL)
    assert _blocks_toml(blocks) == snapshot("""\
[providers.azure_0123456789abcdef0123456789abcdef]
backend = "azure"
default_region = "eastus"
default_vm_size = "Standard_B2s"
resource_group = "mngr-0123456789abcdef0123456789abcdef-eastus"
install_gvisor_runtime = true
docker_runtime = "runsc"
host_dir = "/home/user/.mngr"
volume_home_path = "/home/user"
host_log_dir = "/var/log/mngr"
subscription_id = "sub-1"
tenant_id = "tenant-1"
use_azure_cli_login = true
""")


def test_modal_local_profile_connection_block_names_the_profile_instead_of_a_token() -> None:
    record = _record("modal", {"modal_profile": "work"}, "local_profile")
    blocks = desired_provider_blocks([], {_USER_ID: [record]}, {}, _CONNECTOR_URL)
    block = blocks[provider_instance_name_for_connection(ConnectionBackend.MODAL, _CONNECTION_ID)]
    assert block["modal_profile"] == "work"
    assert "token_id" not in block and "token_secret" not in block
    assert block["user_id"] == _CONNECTION_ID.get_uuid().hex


@pytest.mark.parametrize(
    ("backend", "placement", "credential_source"),
    [
        # GCP has no local login a connection could name.
        ("gcp", {"default_zone": "us-west1-a", "default_machine_type": "e2-small"}, "local_profile"),
        # A local-profile row whose placement never recorded the login.
        ("aws", {"default_region": "us-west-2", "default_instance_type": "t3.large"}, "local_profile"),
        ("azure", {"default_region": "eastus", "default_vm_size": "s", "resource_group": "rg"}, "local_profile"),
        # A credential source a newer app introduced is read-only here.
        ("modal", {"modal_profile": "work"}, "vault"),
    ],
    ids=["gcp-no-local-form", "aws-no-profile-name", "azure-no-tenant", "unknown-source"],
)
def test_local_profile_blocks_need_their_login_fields_and_a_known_source(
    backend: str, placement: dict[str, object], credential_source: str
) -> None:
    record = _record(backend, placement, credential_source)
    blocks = desired_provider_blocks(
        [], {_USER_ID: [record]}, {record.doc_id: ConnectionCredentials()}, _CONNECTOR_URL
    )
    assert blocks == {}


def test_connection_block_needs_the_backends_credential_fields() -> None:
    metadata = ConnectionMetadata(backend="modal", alias="m")
    assert (
        connection_provider_block(ConnectionBackend.MODAL, _CONNECTION_ID, metadata, ConnectionCredentials()) is None
    )


@pytest.mark.parametrize(
    ("name", "is_managed"),
    [
        (f"imbue_cloud_{_USER_ID}", True),
        ("imbue_cloud_alice-example-com", True),
        ("aws_" + "a" * 32, True),
        ("modal_" + "b" * 32, True),
        ("byok-aws-mine", True),
        ("imbue_cloud", False),
        ("aws", False),
        ("modal", False),
        ("docker", False),
        ("my-own-aws", False),
    ],
)
def test_is_managed_provider_name(name: str, is_managed: bool) -> None:
    assert is_managed_provider_name(name) is is_managed


def test_reconcile_writes_managed_blocks_whole_deletes_stale_ones_and_leaves_foreign_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    stale_connection_name = f"aws_{uuid4().hex}"
    settings_path.write_text(
        "\n".join(
            [
                "[providers.docker]",
                "is_enabled = false",
                "[providers.my-own-aws]",
                'backend = "aws"',
                "[providers.byok-aws-mine]",
                'backend = "aws"',
                "[providers.imbue_cloud_alice-example-com]",
                'backend = "imbue_cloud"',
                f"[providers.{stale_connection_name}]",
                'backend = "aws"',
                f"[providers.imbue_cloud_{_USER_ID}]",
                'backend = "imbue_cloud"',
                'stale_key = "goes away"',
                "",
            ]
        )
    )
    desired = desired_provider_blocks([_ACCOUNT], {}, {}, _CONNECTOR_URL)

    result = reconcile_provider_settings(_ROOT, desired)

    assert result.is_changed is True
    # The deleted managed blocks are reported so discovery drops their hosts; the user's own block is not among them.
    assert sorted(result.removed_instance_names) == sorted(
        ["byok-aws-mine", "imbue_cloud_alice-example-com", stale_connection_name]
    )
    parsed = tomllib.loads(settings_path.read_text())
    assert sorted(parsed["providers"]) == ["docker", f"imbue_cloud_{_USER_ID}", "my-own-aws"]
    assert "stale_key" not in parsed["providers"][f"imbue_cloud_{_USER_ID}"]
    assert parsed["providers"]["my-own-aws"] == {"backend": "aws"}
    # Converged: a second reconcile is a no-op writer.
    assert reconcile_provider_settings(_ROOT, desired).is_changed is False


def test_reconcile_keeps_every_block_when_the_providers_tables_are_out_of_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A [providers.*] table after another section makes tomlkit hand back a proxy, not a Table; nothing may be lost."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    settings_path.write_text(
        "\n".join(
            [
                "[providers.imbue_cloud]",
                'backend = "imbue_cloud"',
                "is_enabled = false",
                "[providers.byok-aws-old]",
                'backend = "aws"',
                "[plugins.recursive]",
                "is_enabled = false",
                "[providers.my-own-vultr]",
                'backend = "vultr"',
                "",
            ]
        )
    )
    desired = desired_provider_blocks([_ACCOUNT], {}, {}, _CONNECTOR_URL)

    assert reconcile_provider_settings(_ROOT, desired).is_changed is True

    parsed = tomllib.loads(settings_path.read_text())
    assert sorted(parsed["providers"]) == ["imbue_cloud", f"imbue_cloud_{_USER_ID}", "my-own-vultr"]
    assert parsed["providers"]["my-own-vultr"] == {"backend": "vultr"}
    assert parsed["plugins"] == {"recursive": {"is_enabled": False}}
    assert reconcile_provider_settings(_ROOT, desired).is_changed is False


def test_reconcile_skips_when_mngr_is_uninitialized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert reconcile_provider_settings(_ROOT, {}).is_changed is False


def test_is_provider_polled_here_reads_enabled_blocks_and_backend_defaults_and_keeps_everything_when_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Discovery drops events for an instance this says is not polled, so a settings file that cannot be parsed
    says everything is polled (after a warning) rather than taking every instance's hosts off the home screen."""
    account_instance = imbue_cloud_provider_name_for_user(_USER_ID)
    gone_instance = f"aws_{uuid4().hex}"
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    # Nothing on disk yet: a backend's own default instance is polled without a block, a derived name is not.
    assert is_provider_polled_here(_ROOT, account_instance) is False
    assert is_provider_polled_here(_ROOT, gone_instance) is False
    assert is_provider_polled_here(_ROOT, "local") is True
    assert is_provider_polled_here(_ROOT, "docker") is True
    assert is_provider_polled_here(_ROOT, "imbue_cloud_slice") is True

    settings_path.write_text(
        _blocks_toml(
            {
                account_instance: {"backend": "imbue_cloud"},
                # The five suppressed defaults the pre-import reconcile pins.
                "modal": {"backend": "modal", "is_enabled": False},
                "vultr": {"backend": "vultr", "is_enabled": True},
            }
        )
    )
    assert is_provider_polled_here(_ROOT, account_instance) is True
    assert is_provider_polled_here(_ROOT, gone_instance) is False
    assert is_provider_polled_here(_ROOT, "modal") is False
    assert is_provider_polled_here(_ROOT, "vultr") is True

    settings_path.write_text("[providers\nthis is not toml")
    assert is_provider_polled_here(_ROOT, gone_instance) is True
    assert is_provider_polled_here(_ROOT, "modal") is True


def test_migrate_imbue_cloud_state_dirs_renames_the_email_slug_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    state_root = settings_path.parent / "providers" / "imbue_cloud"
    legacy_dir = state_root / legacy_imbue_cloud_provider_name_for_account(_EMAIL)
    host_dir = legacy_dir / "hosts" / f"host-{uuid4().hex}"
    host_dir.mkdir(parents=True)
    (host_dir / "ssh_key").write_text("key")
    other_email = "bob@example.com"
    other_legacy_dir = state_root / legacy_imbue_cloud_provider_name_for_account(other_email)
    other_legacy_dir.mkdir(parents=True)

    migrate_imbue_cloud_state_dirs(_ROOT, [_ACCOUNT])

    target_dir = state_root / imbue_cloud_provider_name_for_user(_USER_ID)
    assert not legacy_dir.exists()
    assert (target_dir / "hosts" / host_dir.name / "ssh_key").read_text() == "key"
    # An account that is not signed in keeps its legacy directory.
    assert other_legacy_dir.is_dir()
    # Idempotent, and a re-appearing legacy dir never displaces the user-id one.
    legacy_dir.mkdir()
    migrate_imbue_cloud_state_dirs(_ROOT, [_ACCOUNT])
    assert legacy_dir.is_dir() and target_dir.is_dir()


def test_read_signed_in_accounts_from_index_skips_incomplete_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    assert read_signed_in_accounts_from_index(_ROOT) == []
    write_imbue_cloud_accounts_index(
        _ROOT_NAME, [{"email": _EMAIL, "user_id": _USER_ID}, {"email": "no-id@example.com"}, "garbage"]
    )
    assert read_signed_in_accounts_from_index(_ROOT) == [_ACCOUNT]


def test_read_signed_in_accounts_from_index_refuses_an_unreadable_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    write_imbue_cloud_accounts_index(_ROOT_NAME, [{"email": _EMAIL, "user_id": _USER_ID}])
    accounts_path = imbue_cloud_accounts_index_path(_ROOT)
    assert accounts_path is not None
    accounts_path.write_text("{not json")
    with pytest.raises(AccountsIndexUnreadableError):
        read_signed_in_accounts_from_index(_ROOT)
    accounts_path.write_text(json.dumps({"entries": "nope"}))
    with pytest.raises(AccountsIndexUnreadableError):
        read_signed_in_accounts_from_index(_ROOT)


def test_materializer_lands_the_pre_import_shape_a_fresh_profile_missed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The startup ensure no-ops before mngr creates the profile, so the first materialize must land it too.

    Without the suppression blocks, discovery auto-creates a phantom default
    ``imbue_cloud`` instance (no connector_url) that errors on every cycle.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    settings_path.write_text("")
    store = make_connection_store_for_test(tmp_path / f".{_ROOT_NAME}")
    materializer = make_provider_materializer_for_test(_ROOT_NAME, store, lambda: [_ACCOUNT], _CONNECTOR_URL)

    assert materializer.materialize() is ProviderMaterializationOutcome.CHANGED

    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud"] == {"backend": "imbue_cloud", "is_enabled": False}
    assert parsed["providers"]["modal"] == {"backend": "modal", "is_enabled": False}
    assert parsed["plugins"]["recursive"]["enabled"] is False
    assert imbue_cloud_provider_name_for_user(_USER_ID) in parsed["providers"]
    assert materializer.materialize() is ProviderMaterializationOutcome.UNCHANGED


def test_materializer_writes_account_and_connection_blocks_and_locked_accounts_keep_only_their_account_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    data_dir = tmp_path / f".{_ROOT_NAME}"
    store = make_connection_store_for_test(data_dir)
    unlock_account_with_master_password_for_test(store.paths, _USER_ID)
    connection_id = add_modal_connection_for_test(store, _USER_ID, "my modal", token_id="ak-1", token_secret="as-1")
    local_id = ConnectionId.generate()
    store.add_connection(
        LOCAL_PROFILE_USER_ID,
        local_id,
        ConnectionBackend.AWS,
        "device aws",
        "AKIA…",
        {"default_region": "us-east-1", "default_instance_type": "t3.large"},
        ConnectionCredentials(aws_access_key_id="AKIA", aws_secret_access_key=SecretStr("s")),
    )
    signed_in_accounts: list[SignedInAccount] = [_ACCOUNT]
    materializer = make_provider_materializer_for_test(
        _ROOT_NAME, store, lambda: list(signed_in_accounts), _CONNECTOR_URL
    )
    modal_instance = provider_instance_name_for_connection(ConnectionBackend.MODAL, connection_id)
    aws_instance = provider_instance_name_for_connection(ConnectionBackend.AWS, local_id)

    assert materializer.materialize() is ProviderMaterializationOutcome.CHANGED
    providers = tomllib.loads(settings_path.read_text())["providers"]
    assert sorted(_managed_names(providers)) == sorted(
        [imbue_cloud_provider_name_for_user(_USER_ID), modal_instance, aws_instance]
    )
    assert providers[modal_instance]["token_secret"] == "as-1"
    assert materializer.materialize() is ProviderMaterializationOutcome.UNCHANGED

    # Locked: the account instance stays, its pasted-credentials connection goes; the local
    # profile is unaffected, and so is the account's profile-based connection, which no DEK guards.
    profile_id = ConnectionId.generate()
    store.add_connection(
        _USER_ID,
        profile_id,
        ConnectionBackend.MODAL,
        "profile modal",
        "profile work",
        {"modal_profile": "work"},
        None,
        ConnectionCredentialSource.LOCAL_PROFILE,
    )
    profile_instance = provider_instance_name_for_connection(ConnectionBackend.MODAL, profile_id)
    delete_dek(store.paths, _USER_ID)
    assert materializer.materialize() is ProviderMaterializationOutcome.CHANGED
    providers = tomllib.loads(settings_path.read_text())["providers"]
    assert sorted(_managed_names(providers)) == sorted(
        [imbue_cloud_provider_name_for_user(_USER_ID), aws_instance, profile_instance]
    )
    assert providers[profile_instance]["modal_profile"] == "work"

    # Signed out: nothing of the account remains.
    signed_in_accounts.clear()
    assert materializer.materialize() is ProviderMaterializationOutcome.CHANGED
    assert _managed_names(tomllib.loads(settings_path.read_text())["providers"]) == [aws_instance]


def test_materializer_bounces_discovery_only_on_a_change_and_never_from_the_startup_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    store = make_connection_store_for_test(tmp_path / f".{_ROOT_NAME}")
    bounce_count = [0]
    signed_in_accounts: list[SignedInAccount] = [_ACCOUNT]
    materializer = ProviderSettingsMaterializer(
        root_name=_ROOT_NAME,
        connection_store=store,
        connector_url=_CONNECTOR_URL,
        list_accounts=lambda: list(signed_in_accounts),
        on_settings_changed=lambda: bounce_count.__setitem__(0, bounce_count[0] + 1),
    )

    # Startup writes the whole provider set before any observer exists: no bounce.
    assert materializer.materialize_without_bounce(lambda: [_ACCOUNT]) is ProviderMaterializationOutcome.CHANGED
    assert bounce_count[0] == 0
    # A converged write bounces nothing; a changed one bounces once.
    assert materializer.materialize() is ProviderMaterializationOutcome.UNCHANGED
    assert bounce_count[0] == 0
    signed_in_accounts.clear()
    assert materializer.materialize() is ProviderMaterializationOutcome.CHANGED
    assert bounce_count[0] == 1


def test_materializer_reports_the_instances_a_write_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Discovery learns which provider instances went away (a signout here) so their hosts are forgotten."""
    stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    store = make_connection_store_for_test(tmp_path / f".{_ROOT_NAME}")
    removed: list[frozenset[str]] = []
    signed_in_accounts: list[SignedInAccount] = [_ACCOUNT]
    materializer = ProviderSettingsMaterializer(
        root_name=_ROOT_NAME,
        connection_store=store,
        connector_url=_CONNECTOR_URL,
        list_accounts=lambda: list(signed_in_accounts),
        on_provider_instances_removed=lambda names: removed.append(frozenset(names)),
    )
    assert materializer.materialize() is ProviderMaterializationOutcome.CHANGED
    assert removed == []

    signed_in_accounts.clear()
    assert materializer.materialize() is ProviderMaterializationOutcome.CHANGED

    assert removed == [frozenset({f"imbue_cloud_{_USER_ID}"})]


def test_materializer_leaves_the_settings_alone_when_the_account_listing_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    store = make_connection_store_for_test(tmp_path / f".{_ROOT_NAME}")
    listing: list[list[SignedInAccount] | None] = [[_ACCOUNT]]
    materializer = make_provider_materializer_for_test(_ROOT_NAME, store, lambda: listing[0], _CONNECTOR_URL)
    assert materializer.materialize() is ProviderMaterializationOutcome.CHANGED
    written = settings_path.read_text()

    listing[0] = None
    assert materializer.materialize() is ProviderMaterializationOutcome.SKIPPED
    assert settings_path.read_text() == written
