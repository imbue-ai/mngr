"""Integration tests for the minds-side mngr settings: the startup materializer and expired sessions.

The pre-import reconcile (``mngr_settings/reconcile.py``) lands the suppression
blocks before mngr loads; the materializer (``desktop_client/connections/
provider_settings.py``) then derives every account and connection instance
from the plugin's session index and the local replicas. These tests drive both
from representative on-disk states and pin the resulting ``settings.toml``.
The last test runs a real ``mngr observe`` against a materialized account block
whose connector refuses the session refresh, closing the loop from the plugin's
auth error to the sync pass that retires the account.
"""

import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from inline_snapshot import snapshot
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.bootstrap import MindsRoot
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import add_modal_connection_for_test
from imbue.minds.desktop_client.conftest import make_connection_store_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_provider_materializer_for_test
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.conftest import seed_provider_snapshots
from imbue.minds.desktop_client.conftest import session_store_account_source
from imbue.minds.desktop_client.conftest import unlock_account_with_master_password_for_test
from imbue.minds.desktop_client.connections.account_lifecycle import sign_out_account_locally
from imbue.minds.desktop_client.connections.account_lifecycle import sign_out_account_via_plugin
from imbue.minds.desktop_client.connections.account_lifecycle import sign_out_dead_account
from imbue.minds.desktop_client.connections.connection_store import ConnectionStore
from imbue.minds.desktop_client.connections.data_types import CONNECTION_STATE_DELETED
from imbue.minds.desktop_client.connections.data_types import ConnectionCredentials
from imbue.minds.desktop_client.connections.data_types import ProviderMaterializationOutcome
from imbue.minds.desktop_client.connections.data_types import SignedInAccount
from imbue.minds.desktop_client.connections.primitives import CONNECTION_DOCUMENT_KIND
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import ConnectionId
from imbue.minds.desktop_client.connections.primitives import imbue_cloud_provider_name_for_user
from imbue.minds.desktop_client.connections.primitives import provider_instance_name_for_connection
from imbue.minds.desktop_client.connections.provider_settings import ProviderSettingsMaterializer
from imbue.minds.desktop_client.connections.provider_settings import read_signed_in_accounts_from_index
from imbue.minds.desktop_client.connections.provider_settings import read_signed_in_accounts_from_index_or_none
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.imbue_cloud_cli import AuthRefreshOutcome
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.mock_agent_creator_test import InFlightCreateAttempt
from imbue.minds.desktop_client.mock_agent_creator_test import make_in_flight_agent_creator
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sync_scheduler import SESSION_EXPIRED_ERROR_TYPE_NAMES
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.mngr_settings.reconcile import ensure_mngr_settings
from imbue.minds.testing import stub_mngr_host_dir
from imbue.minds.testing import write_imbue_cloud_accounts_index
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.config import get_sessions_dir
from imbue.mngr_imbue_cloud.connector.session_store import ImbueCloudSessionStore
from imbue.mngr_imbue_cloud.data_types import AuthSession
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId

_ROOT_NAME = "minds-dev-tname"
_ROOT = MindsRoot(_ROOT_NAME)
_CONNECTOR_URL = "https://test--rsc-api.modal.run"
_USER_ID = "3f2a9c1e-5b7d-4a8e-9c0f-1d2e3f4a5b6c"
_EMAIL = "alice@example.com"
_CONNECTION_ID = ConnectionId("conn-0123456789abcdef0123456789abcdef")

# The pre-import shape every ensure produces in a fresh profile: the suppressed
# default provider instances and the recursive-plugin disable.
_BASE_RECONCILED_SHAPE = snapshot(
    {
        "providers": {
            "imbue_cloud": {"backend": "imbue_cloud", "is_enabled": False},
            "aws": {"backend": "aws", "is_enabled": False},
            "gcp": {"backend": "gcp", "is_enabled": False},
            "azure": {"backend": "azure", "is_enabled": False},
            "modal": {"backend": "modal", "is_enabled": False},
        },
        "plugins": {"recursive": {"enabled": False}},
        # Destroyed mngr host records age out with the 30-day backup retention
        # window in profiles Imbue Studio manages (mngr's own default is 7 days).
        "default_destroyed_host_persisted_seconds": 60 * 60 * 24 * 30,
    }
)


def _parsed(settings_path: Path) -> dict:
    return tomllib.loads(settings_path.read_text())


def _write_accounts_index(accounts: list[tuple[str, str]]) -> None:
    """Write the plugin's accounts.json under the (HOME-stubbed) active profile dir."""
    write_imbue_cloud_accounts_index(_ROOT_NAME, [{"email": email, "user_id": user_id} for user_id, email in accounts])


def test_ensure_skips_silently_when_mngr_uninitialized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ensure_mngr_settings(_ROOT) is False
    assert not mngr_host_dir_for(_ROOT_NAME).exists()


def test_ensure_writes_desired_shape_into_fresh_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    assert ensure_mngr_settings(_ROOT) is True
    assert _parsed(settings_path) == _BASE_RECONCILED_SHAPE


def test_ensure_is_idempotent_after_first_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    assert ensure_mngr_settings(_ROOT) is True
    first_write = settings_path.read_text()
    assert ensure_mngr_settings(_ROOT) is False
    assert settings_path.read_text() == first_write


def test_ensure_cleans_legacy_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    # A settings file bearing every category of legacy residue: the ssh
    # provider block, ambient per-region aws blocks, and the ambient Modal
    # DIRECT block (with a panel-written enable) from before Modal became a
    # connection.
    settings_path.write_text(
        "\n".join(
            [
                "[providers.ssh]",
                'backend = "ssh"',
                "[providers.aws-us-east-1]",
                'backend = "aws"',
                'default_region = "us-east-1"',
                "[providers.aws-eu-west-1]",
                'backend = "aws"',
                'default_region = "eu-west-1"',
                "[providers.modal]",
                'backend = "modal"',
                'mode = "DIRECT"',
                "is_enabled = true",
                "is_persistent = false",
                "",
            ]
        )
    )
    # The state root, which is where the reconciler now looks for this residue.
    data_dir = _ROOT.state_dir
    dynamic_hosts_path = data_dir / "ssh" / "dynamic_hosts.toml"
    dynamic_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    dynamic_hosts_path.write_text("[hosts]\n")
    leased_key_dir = data_dir / "ssh" / "keys" / "leased_host"
    leased_key_dir.mkdir(parents=True, exist_ok=True)
    (leased_key_dir / "id_ed25519").write_text("stale key\n")

    assert ensure_mngr_settings(_ROOT) is True
    assert _parsed(settings_path) == _BASE_RECONCILED_SHAPE
    assert not dynamic_hosts_path.exists()
    assert not leased_key_dir.exists()


class _SignedInDevice(FrozenModel):
    """One device with the test account signed in on the fake plugin, its settings reconciled and materialized."""

    settings_path: Path
    data_dir: Path
    cli: FakeImbueCloudCli
    session_store: MultiAccountSessionStore
    store: ConnectionStore
    materializer: ProviderSettingsMaterializer


def _signed_in_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _SignedInDevice:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    data_dir = tmp_path / f".{_ROOT_NAME}"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    store = make_connection_store_for_test(data_dir, cli)
    session_store = make_session_store_for_test(data_dir, cli)
    materializer = make_provider_materializer_for_test(
        _ROOT_NAME, store, session_store_account_source(session_store), _CONNECTOR_URL
    )
    ensure_mngr_settings(_ROOT)
    materializer.materialize()
    return _SignedInDevice(
        settings_path=settings_path,
        data_dir=data_dir,
        cli=cli,
        session_store=session_store,
        store=store,
        materializer=materializer,
    )


def test_startup_materializes_accounts_and_connections_from_the_index_and_replica(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The startup path: pre-import reconcile, then the materializer over the session index + replicas.

    A signed-in, unlocked account with one synced Modal connection and a local-profile
    AWS connection yields the account instance plus both connection instances, next to
    the suppression blocks; a legacy email-slug block and byok block are removed and the
    legacy state dir is renamed.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    settings_path.write_text(
        "\n".join(
            [
                "[providers.imbue_cloud_alice-example-com]",
                'backend = "imbue_cloud"',
                "[providers.byok-aws-old]",
                'backend = "aws"',
                "",
            ]
        )
    )
    legacy_state_dir = settings_path.parent / "providers" / "imbue_cloud" / "imbue_cloud_alice-example-com"
    (legacy_state_dir / "hosts").mkdir(parents=True)
    _write_accounts_index([(_USER_ID, _EMAIL)])
    data_dir = tmp_path / f".{_ROOT_NAME}"
    store = make_connection_store_for_test(data_dir)
    unlock_account_with_master_password_for_test(store.paths, _USER_ID)
    add_modal_connection_for_test(
        store, _USER_ID, "my modal", connection_id=_CONNECTION_ID, token_id="ak-1", token_secret="as-1"
    )
    local_id = ConnectionId("conn-fedcba9876543210fedcba9876543210")
    store.add_connection(
        "local",
        local_id,
        ConnectionBackend.AWS,
        "device aws",
        "AKIA…PLE9",
        {"default_region": "us-east-1", "default_instance_type": "t3.large"},
        ConnectionCredentials(aws_access_key_id="AKIA1234EXAMPLE9", aws_secret_access_key=SecretStr("s")),
    )
    materializer = make_provider_materializer_for_test(
        _ROOT_NAME, store, lambda: read_signed_in_accounts_from_index_or_none(_ROOT), _CONNECTOR_URL
    )

    assert ensure_mngr_settings(_ROOT) is True
    assert read_signed_in_accounts_from_index(_ROOT) == [SignedInAccount(user_id=_USER_ID, email=_EMAIL)]
    assert (
        materializer.materialize_without_bounce(lambda: read_signed_in_accounts_from_index_or_none(_ROOT))
        is ProviderMaterializationOutcome.CHANGED
    )

    assert _parsed(settings_path)["providers"] == snapshot(
        {
            "imbue_cloud": {"backend": "imbue_cloud", "is_enabled": False},
            "aws": {"backend": "aws", "is_enabled": False},
            "gcp": {"backend": "gcp", "is_enabled": False},
            "azure": {"backend": "azure", "is_enabled": False},
            "modal": {"backend": "modal", "is_enabled": False},
            "imbue_cloud_3f2a9c1e-5b7d-4a8e-9c0f-1d2e3f4a5b6c": {
                "backend": "imbue_cloud",
                "account": "alice@example.com",
                "connector_url": "https://test--rsc-api.modal.run",
                "docker_runtime": "runsc",
                "install_gvisor_runtime": True,
                "default_start_args": ["--workdir=/", "--security-opt=no-new-privileges"],
                "host_dir": "/home/user/.mngr",
                "volume_home_path": "/home/user",
                "host_log_dir": "/var/log/mngr",
            },
            "modal_0123456789abcdef0123456789abcdef": {
                "backend": "modal",
                "mode": "DIRECT",
                "is_persistent": True,
                "user_id": "0123456789abcdef0123456789abcdef",
                "default_cpu": 2.0,
                "default_memory": 4.0,
                "default_sandbox_timeout": 86310,
                "default_idle_timeout": 86400,
                "token_id": "ak-1",
                "token_secret": "as-1",
            },
            "aws_fedcba9876543210fedcba9876543210": {
                "backend": "aws",
                "default_region": "us-east-1",
                "default_instance_type": "t3.large",
                "install_gvisor_runtime": True,
                "docker_runtime": "runsc",
                "host_dir": "/home/user/.mngr",
                "volume_home_path": "/home/user",
                "host_log_dir": "/var/log/mngr",
                "aws_access_key_id": "AKIA1234EXAMPLE9",
                "aws_secret_access_key": "s",
            },
        }
    )
    assert not legacy_state_dir.exists()
    assert (settings_path.parent / "providers" / "imbue_cloud" / f"imbue_cloud_{_USER_ID}" / "hosts").is_dir()
    # Converged: the next startup writes nothing.
    assert ensure_mngr_settings(_ROOT) is False
    assert materializer.materialize() is ProviderMaterializationOutcome.UNCHANGED


def test_signout_removes_the_accounts_instances_and_replica_but_keeps_the_dek(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    device = _signed_in_device(tmp_path, monkeypatch)
    unlock_account_with_master_password_for_test(device.store.paths, _USER_ID)
    add_modal_connection_for_test(
        device.store, _USER_ID, "my modal", connection_id=_CONNECTION_ID, token_id="ak-1", token_secret="as-1"
    )
    device.materializer.materialize()
    assert imbue_cloud_provider_name_for_user(_USER_ID) in _parsed(device.settings_path)["providers"]

    device.cli.remove_account(_USER_ID)
    sign_out_account_locally(
        _USER_ID,
        session_store=device.session_store,
        connection_store=device.store,
        provider_materializer=device.materializer,
    )

    providers = _parsed(device.settings_path)["providers"]
    assert imbue_cloud_provider_name_for_user(_USER_ID) not in providers
    assert provider_instance_name_for_connection(ConnectionBackend.MODAL, _CONNECTION_ID) not in providers
    assert device.store.list_connections(_USER_ID) == []
    assert (device.data_dir / "keys" / f"{_USER_ID}.dek").is_file()


def test_signout_pushes_a_connection_removal_still_queued_from_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Signout deletes the replica, so a removal queued while offline must reach the server first."""
    device = _signed_in_device(tmp_path, monkeypatch)
    unlock_account_with_master_password_for_test(device.store.paths, _USER_ID)
    add_modal_connection_for_test(device.store, _USER_ID, "my modal", connection_id=_CONNECTION_ID)
    device.store.push_connection(_USER_ID, _EMAIL, str(_CONNECTION_ID))
    device.cli.is_sync_offline = True
    device.store.remove_connection(_USER_ID, _EMAIL, str(_CONNECTION_ID))
    device.cli.is_sync_offline = False

    sign_out_account_via_plugin(
        _USER_ID,
        cli=device.cli,
        session_store=device.session_store,
        connection_store=device.store,
        provider_materializer=device.materializer,
        minds_config=None,
    )

    stored = device.cli.sync_documents_by_email[_EMAIL][CONNECTION_DOCUMENT_KIND][str(_CONNECTION_ID)]
    assert stored["state"] == CONNECTION_STATE_DELETED
    assert device.store.list_connections(_USER_ID) == []


def test_signout_leaves_the_account_alone_when_the_listing_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed `auth list` reads like a signed-out account, but the account is still signed in: tearing the local
    half down on it would discard the replica (and any queued connection change) for nothing."""
    device = _signed_in_device(tmp_path, monkeypatch)
    unlock_account_with_master_password_for_test(device.store.paths, _USER_ID)
    add_modal_connection_for_test(device.store, _USER_ID, "my modal", connection_id=_CONNECTION_ID)
    device.materializer.materialize()
    device.cli.is_auth_list_failing = True
    device.session_store.invalidate_identity_cache()

    sign_out_account_via_plugin(
        _USER_ID,
        cli=device.cli,
        session_store=device.session_store,
        connection_store=device.store,
        provider_materializer=device.materializer,
        minds_config=None,
    )

    assert device.cli.signed_out_accounts == []
    assert len(device.store.list_connections(_USER_ID)) == 1
    providers = _parsed(device.settings_path)["providers"]
    assert imbue_cloud_provider_name_for_user(_USER_ID) in providers
    assert provider_instance_name_for_connection(ConnectionBackend.MODAL, _CONNECTION_ID) in providers


def _scheduler_with_expired_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: AuthRefreshOutcome,
    agent_creator: AgentCreator | None = None,
) -> tuple[WorkspaceSyncScheduler, FakeImbueCloudCli, Path, list[bool]]:
    """A sync scheduler whose one account's imbue_cloud discovery reports an auth error.

    The dead-session callback is the app's real one. Returns the scheduler,
    the fake CLI, the settings path, and one entry per UI wake the callback
    issued: whether the account's provider block was already gone by then.
    """
    device = _signed_in_device(tmp_path, monkeypatch)
    settings_path, cli, session_store, store, materializer = (
        device.settings_path,
        device.cli,
        device.session_store,
        device.store,
        device.materializer,
    )
    cli.auth_refresh_outcome_to_return = outcome
    assert session_store.record_store is not None
    minds_config = MindsConfig(data_dir=device.data_dir)
    minds_config.set_default_account_id(_USER_ID)
    resolver = MngrCliBackendResolver()
    instance = ProviderInstanceName(imbue_cloud_provider_name_for_user(_USER_ID))
    seed_provider_snapshots(
        resolver,
        providers=(),
        error_by_provider_name={
            instance: DiscoveryError(type_name="ImbueCloudAuthError", message="expired", provider_name=instance)
        },
        last_snapshot_at=datetime.now(timezone.utc),
    )
    is_provider_gone_at_wake: list[bool] = []

    def _record_wake() -> None:
        is_provider_gone_at_wake.append(str(instance) not in _parsed(settings_path)["providers"])

    scheduler = WorkspaceSyncScheduler(
        record_store=session_store.record_store,
        session_store=session_store,
        resolver=resolver,
        connection_store=store,
        provider_materializer=materializer,
        imbue_cloud_cli=cli,
        agent_creator=agent_creator,
        on_account_session_dead=lambda user_id: sign_out_dead_account(
            user_id, cli, session_store, store, materializer, minds_config, _record_wake
        ),
    )
    return scheduler, cli, settings_path, is_provider_gone_at_wake


def test_sync_pass_signs_out_an_account_whose_refresh_the_connector_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A discovery auth error triggers one refresh; a dead refresh token retires the account and its blocks.

    The UI is woken exactly once, after the account's provider block is gone,
    so the re-derived account launcher no longer lists the account.
    """
    scheduler, cli, settings_path, is_provider_gone_at_wake = _scheduler_with_expired_session(
        tmp_path, monkeypatch, AuthRefreshOutcome.SESSION_DEAD
    )

    scheduler.run_one_pass()

    assert cli.refreshed_accounts == [_EMAIL]
    assert cli.signed_out_accounts == [_EMAIL]
    assert imbue_cloud_provider_name_for_user(_USER_ID) not in _parsed(settings_path)["providers"]
    assert is_provider_gone_at_wake == [True]
    # The retired account was the default; nothing is left to take over.
    assert MindsConfig(data_dir=tmp_path / f".{_ROOT_NAME}").get_default_account_id() is None


@pytest.mark.parametrize("is_create_on_the_account", [True, False])
def test_sync_pass_does_not_retire_an_account_with_a_workspace_still_being_created_on_its_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, is_create_on_the_account: bool
) -> None:
    """The retirement would delete the blocks the create's later mngr calls need; a later pass retries it."""
    in_flight_provider = imbue_cloud_provider_name_for_user(_USER_ID) if is_create_on_the_account else "docker"
    with ConcurrencyGroup(name="in-flight-create") as concurrency_group:
        agent_creator = make_in_flight_agent_creator(
            tmp_path,
            concurrency_group,
            (InFlightCreateAttempt(provider_instance_name=in_flight_provider, host_name="still-creating"),),
        )
        scheduler, cli, settings_path, _ = _scheduler_with_expired_session(
            tmp_path, monkeypatch, AuthRefreshOutcome.SESSION_DEAD, agent_creator
        )

        scheduler.run_one_pass()

    is_retired = imbue_cloud_provider_name_for_user(_USER_ID) not in _parsed(settings_path)["providers"]
    assert is_retired is not is_create_on_the_account
    assert cli.refreshed_accounts == ([] if is_create_on_the_account else [_EMAIL])


def test_a_sync_pass_that_cannot_rewrite_the_settings_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pass re-materializes the settings; a broken settings file must not end the sync loop."""
    scheduler, cli, settings_path, _ = _scheduler_with_expired_session(
        tmp_path, monkeypatch, AuthRefreshOutcome.SESSION_DEAD
    )
    settings_path.write_text("[providers.imbue_cloud\n")

    scheduler.run_one_pass_guarded()

    assert cli.signed_out_accounts == [_EMAIL]


@pytest.mark.parametrize("outcome", [AuthRefreshOutcome.TRANSIENT_FAILURE, AuthRefreshOutcome.REFRESHED])
def test_sync_pass_keeps_an_account_whose_refresh_did_not_prove_the_session_dead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: AuthRefreshOutcome
) -> None:
    """A transient connector failure (or a successful refresh) never signs an account out."""
    scheduler, cli, settings_path, is_provider_gone_at_wake = _scheduler_with_expired_session(
        tmp_path, monkeypatch, outcome
    )

    scheduler.run_one_pass()
    # The refresh is not retried on every pass while the errored snapshot lingers.
    scheduler.run_one_pass()

    assert cli.refreshed_accounts == [_EMAIL]
    assert cli.signed_out_accounts == []
    assert is_provider_gone_at_wake == []
    assert imbue_cloud_provider_name_for_user(_USER_ID) in _parsed(settings_path)["providers"]
    assert MindsConfig(data_dir=tmp_path / f".{_ROOT_NAME}").get_default_account_id() == _USER_ID


def test_sync_pass_pulls_connections_and_materializes_when_a_row_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A connection synced from another device shows up as a block after the pull (the account is unlocked here)."""
    device = _signed_in_device(tmp_path, monkeypatch)
    settings_path, cli, session_store = device.settings_path, device.cli, device.session_store
    assert session_store.record_store is not None
    # Device A: add + push through its own store (sharing the fake connector).
    device_a = make_connection_store_for_test(tmp_path / "device-a", cli)
    unlock_account_with_master_password_for_test(device_a.paths, _USER_ID)
    add_modal_connection_for_test(
        device_a, _USER_ID, "my modal", connection_id=_CONNECTION_ID, token_id="ak-1", token_secret="as-1"
    )
    device_a.push_connection(_USER_ID, _EMAIL, str(_CONNECTION_ID))
    # Device B: same DEK (an unlocked account), no replica yet.
    store, materializer = device.store, device.materializer
    (device.data_dir / "keys").mkdir(parents=True, exist_ok=True)
    (device.data_dir / "keys" / f"{_USER_ID}.dek").write_bytes(
        (device_a.paths.data_dir / "keys" / f"{_USER_ID}.dek").read_bytes()
    )
    scheduler = WorkspaceSyncScheduler(
        record_store=session_store.record_store,
        session_store=session_store,
        resolver=MngrCliBackendResolver(),
        connection_store=store,
        provider_materializer=materializer,
        imbue_cloud_cli=cli,
    )

    scheduler.run_one_pass()

    providers = _parsed(settings_path)["providers"]
    modal_instance = provider_instance_name_for_connection(ConnectionBackend.MODAL, _CONNECTION_ID)
    assert providers[modal_instance]["token_id"] == "ak-1"
    assert imbue_cloud_provider_name_for_user(_USER_ID) in providers


def test_sync_pass_does_not_write_back_an_account_that_signed_out_during_its_pulls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The materializer lists the accounts itself, under its lock, when the pass reaches it.

    The pass snapshots the account list before its network pulls; an account
    signed out meanwhile (here: from inside the fake connector's pull, the
    moment the pass is busy) must not have its block written back from that
    stale snapshot, or it would error on every discovery poll until the next
    trigger. The document pulled for the other account is what a pass used to
    re-materialize on at all.
    """
    device = _signed_in_device(tmp_path, monkeypatch)
    other_user_id = "9b8c7d6e-5f4a-4b3c-8d2e-1f0a9b8c7d6e"
    other_email = "bob@example.com"
    device.cli.add_account(user_id=other_user_id, email=other_email)
    device.session_store.invalidate_identity_cache()
    device.materializer.materialize()
    assert imbue_cloud_provider_name_for_user(other_user_id) in _parsed(device.settings_path)["providers"]
    device.cli.seed_connection_document(
        _EMAIL, str(_CONNECTION_ID), {"backend": "modal", "alias": "synced modal", "identifier": "", "placement": {}}
    )
    assert device.session_store.record_store is not None
    minds_config = MindsConfig(data_dir=device.data_dir)

    def _sign_out_bob_mid_pull(account: str) -> None:
        if account == _EMAIL:
            sign_out_account_via_plugin(
                other_user_id,
                cli=device.cli,
                session_store=device.session_store,
                connection_store=device.store,
                provider_materializer=device.materializer,
                minds_config=minds_config,
            )

    device.cli.on_sync_documents_pull = _sign_out_bob_mid_pull
    scheduler = WorkspaceSyncScheduler(
        record_store=device.session_store.record_store,
        session_store=device.session_store,
        resolver=MngrCliBackendResolver(),
        connection_store=device.store,
        provider_materializer=device.materializer,
        imbue_cloud_cli=device.cli,
    )

    scheduler.run_one_pass()

    providers = _parsed(device.settings_path)["providers"]
    assert device.cli.signed_out_accounts == [other_email]
    assert imbue_cloud_provider_name_for_user(other_user_id) not in providers
    assert imbue_cloud_provider_name_for_user(_USER_ID) in providers
    assert [record.doc_id for record in device.store.list_active_connections(_USER_ID)] == [str(_CONNECTION_ID)]


class _RefusingRefreshConnectorHandler(BaseHTTPRequestHandler):
    """A loopback connector that refuses every session refresh the way the real one does: ``200 {"status": "ERROR"}``."""

    refresh_count: int = 0
    lock: threading.Lock = threading.Lock()

    def do_POST(self) -> None:
        # Drain the request body before answering: the handler closes the
        # connection after its HTTP/1.0 response, and a socket closed with
        # unread bytes is reset rather than shut down, which the plugin can see
        # as a connection reset before it reads the 200.
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path != "/auth/session/refresh":
            self.send_response(404)
            self.end_headers()
            return
        with type(self).lock:
            type(self).refresh_count += 1
        body = json.dumps({"status": "ERROR", "message": "session expired"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _refusing_refresh_connector() -> Iterator[str]:
    """Serve the refusing connector on a loopback port; yields its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RefusingRefreshConnectorHandler)
    thread = threading.Thread(target=server.serve_forever, name="refusing-connector", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


# Every other backend this install registers gets a default instance that discovery would poll;
# the observe child under test only needs the account instance, and a docker or lima poll in a
# sandbox without those daemons would only add noise and time.
_OTHER_DEFAULT_PROVIDER_NAMES = ("docker", "lima", "local", "vultr", "ovh", "ssh", "vps_docker", "imbue_cloud_slice")


@contextmanager
def _streaming_discovery(
    mngr_host_dir: Path, home: Path, stderr_path: Path, consumer: EnvelopeStreamConsumer
) -> Iterator[subprocess.Popen[str]]:
    """Run the venv's real ``mngr observe --discovery-only`` against the stubbed host dir, feeding its stdout
    into ``consumer`` the way ``mngr forward --observe-via-file`` envelopes the shared discovery log."""
    environment = {**os.environ, "HOME": str(home), "MNGR_HOST_DIR": str(mngr_host_dir)}
    with stderr_path.open("w") as stderr_file:
        process = subprocess.Popen(
            [str(Path(sys.executable).with_name("mngr")), "observe", "--discovery-only", "--quiet"],
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            env=environment,
        )

        def _feed_consumer() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                if line.strip():
                    consumer._handle_envelope_line(json.dumps({"stream": "observe", "payload": json.loads(line)}))

        reader = threading.Thread(target=_feed_consumer, name="observe-stdout-reader", daemon=True)
        reader.start()
        try:
            yield process
        finally:
            process.terminate()
            try:
                process.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15.0)
            reader.join(timeout=5.0)


@pytest.mark.timeout(120)
def test_a_refused_session_refresh_reaches_the_sync_pass_through_a_real_observe_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The plugin's auth error survives the observe boundary with the type name the sync pass matches.

    A materialized account block whose ``connector_url`` is a loopback server that
    refuses the refresh (the connector's ``200 {"status": "ERROR"}``) makes the real
    ``mngr observe`` child's imbue_cloud poll raise the plugin's own
    ``ImbueCloudAuthError``; its error snapshot is consumed by the real envelope
    consumer into the resolver, and the pass that reads the resolver retires the
    account once the plugin (the fake here) reports the refresh refused.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    data_dir = tmp_path / f".{_ROOT_NAME}"
    with _refusing_refresh_connector() as connector_url:
        # The plugin's own session for the account, with an access token past its expiry so the
        # first discovery poll must refresh it.
        ImbueCloudSessionStore(sessions_dir=get_sessions_dir(settings_path.parent)).save(
            AuthSession(
                user_id=SuperTokensUserId(_USER_ID),
                email=ImbueCloudAccount(_EMAIL),
                access_token=SecretStr("expired-access-token"),
                refresh_token=SecretStr("dead-refresh-token"),
                access_token_expires_at=None,
            )
        )
        cli = make_fake_imbue_cloud_cli()
        cli.add_account(user_id=_USER_ID, email=_EMAIL)
        cli.auth_refresh_outcome_to_return = AuthRefreshOutcome.SESSION_DEAD
        store = make_connection_store_for_test(data_dir, cli)
        session_store = make_session_store_for_test(data_dir, cli)
        materializer = make_provider_materializer_for_test(
            _ROOT_NAME, store, session_store_account_source(session_store), connector_url
        )
        assert ensure_mngr_settings(_ROOT) is True
        assert materializer.materialize() is ProviderMaterializationOutcome.CHANGED
        # The child is a real mngr, which refuses a config under pytest unless it opts in.
        settings_path.write_text(
            "is_allowed_in_pytest = true\n"
            + settings_path.read_text()
            + "".join(f"[providers.{name}]\nis_enabled = false\n" for name in _OTHER_DEFAULT_PROVIDER_NAMES)
        )
        instance = ProviderInstanceName(imbue_cloud_provider_name_for_user(_USER_ID))
        assert str(instance) in _parsed(settings_path)["providers"]
        resolver = MngrCliBackendResolver()
        consumer = EnvelopeStreamConsumer(resolver=resolver)
        stderr_path = tmp_path / "observe-stderr.txt"

        with _streaming_discovery(mngr_host_dir_for(_ROOT_NAME), tmp_path, stderr_path, consumer):
            deadline = time.monotonic() + 90.0
            while instance not in resolver.get_provider_errors() and time.monotonic() < deadline:
                threading.Event().wait(0.25)
            error = resolver.get_provider_errors().get(instance)
            assert error is not None, f"no error snapshot for {instance}; observe stderr:\n{stderr_path.read_text()}"

        assert error.type_name in SESSION_EXPIRED_ERROR_TYPE_NAMES, error
        assert error.type_name == "ImbueCloudAuthError"
        assert _RefusingRefreshConnectorHandler.refresh_count >= 1

        minds_config = MindsConfig(data_dir=data_dir)
        minds_config.set_default_account_id(_USER_ID)
        assert session_store.record_store is not None
        scheduler = WorkspaceSyncScheduler(
            record_store=session_store.record_store,
            session_store=session_store,
            resolver=resolver,
            connection_store=store,
            provider_materializer=materializer,
            imbue_cloud_cli=cli,
            on_account_session_dead=lambda user_id: sign_out_dead_account(
                user_id, cli, session_store, store, materializer, minds_config, lambda: None
            ),
        )

        scheduler.run_one_pass()

        assert cli.refreshed_accounts == [_EMAIL]
        assert cli.signed_out_accounts == [_EMAIL]
        assert str(instance) not in _parsed(settings_path)["providers"]
