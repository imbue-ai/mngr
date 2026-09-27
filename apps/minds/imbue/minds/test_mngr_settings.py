"""Characterization tests for the minds-side mngr settings reconciliation.

These pin the exact settings.toml structure the bootstrap produces from
representative starting states, so the refactor of the reconciliation
logic can be verified to be behavior-preserving at each step.
"""

import json
import tomllib
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.minds.bootstrap import MindsRoot
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.mngr_settings.byok_accounts import delete_cloud_account_provider
from imbue.minds.mngr_settings.byok_accounts import list_cloud_account_providers
from imbue.minds.mngr_settings.byok_accounts import set_cloud_account_provider
from imbue.minds.mngr_settings.enablement import set_provider_is_enabled
from imbue.minds.mngr_settings.imbue_cloud_accounts import reconcile_imbue_cloud_providers_from_sessions
from imbue.minds.mngr_settings.imbue_cloud_accounts import set_imbue_cloud_provider_for_account
from imbue.minds.mngr_settings.imbue_cloud_accounts import unset_imbue_cloud_provider_for_account
from imbue.minds.mngr_settings.reconcile import ensure_mngr_settings
from imbue.minds.testing import stub_mngr_host_dir

_ROOT_NAME = "minds-dev-tname"
_ROOT = MindsRoot(_ROOT_NAME)
_CONNECTOR_URL = "https://test--rsc-api.modal.run"

# The reconciled shape every ensure produces in a fresh profile: the four
# suppressed default provider instances, the Modal (DIRECT) block, and the
# recursive-plugin disable.
_BASE_RECONCILED_SHAPE = snapshot(
    {
        "providers": {
            "imbue_cloud": {"backend": "imbue_cloud", "is_enabled": False},
            "aws": {"backend": "aws", "is_enabled": False},
            "gcp": {"backend": "gcp", "is_enabled": False},
            "azure": {"backend": "azure", "is_enabled": False},
            "modal": {"backend": "modal", "mode": "DIRECT", "is_enabled": True, "is_persistent": True},
        },
        "plugins": {"recursive": {"enabled": False}},
        # Destroyed mngr host records age out with the 30-day backup retention
        # window in minds-managed profiles (mngr's own default is 7 days).
        "default_destroyed_host_persisted_seconds": 60 * 60 * 24 * 30,
    }
)


def _parsed(settings_path: Path) -> dict:
    return tomllib.loads(settings_path.read_text())


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
    # provider block, ambient per-region aws blocks, and a Modal block from a
    # build that wrote is_persistent=false.
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
    data_dir = tmp_path / f".{_ROOT_NAME}"
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


def test_ensure_preserves_panel_disabled_modal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    assert ensure_mngr_settings(_ROOT) is True
    assert set_provider_is_enabled("modal", False, root=_ROOT) is True
    # The disabled Modal block is a valid desired shape, not drift: a second
    # ensure must neither rewrite the file nor re-enable the provider.
    assert ensure_mngr_settings(_ROOT) is False
    assert _parsed(settings_path)["providers"]["modal"]["is_enabled"] is False


def test_signin_then_signout_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    assert (
        set_imbue_cloud_provider_for_account(
            "alice@example.com",
            connector_url=_CONNECTOR_URL,
            root=_ROOT,
        )
        is True
    )
    assert _parsed(settings_path) == snapshot(
        {
            "providers": {
                "imbue_cloud": {"backend": "imbue_cloud", "is_enabled": False},
                "aws": {"backend": "aws", "is_enabled": False},
                "gcp": {"backend": "gcp", "is_enabled": False},
                "azure": {"backend": "azure", "is_enabled": False},
                "modal": {"backend": "modal", "mode": "DIRECT", "is_enabled": True, "is_persistent": True},
                "imbue_cloud_alice-example-com": {
                    "backend": "imbue_cloud",
                    "account": "alice@example.com",
                    "connector_url": _CONNECTOR_URL,
                    "is_enabled": True,
                    "docker_runtime": "runsc",
                    "install_gvisor_runtime": True,
                    "default_start_args": ["--workdir=/", "--security-opt=no-new-privileges"],
                    "host_dir": "/home/user/.mngr",
                    "volume_home_path": "/home/user",
                    "host_log_dir": "/var/log/mngr",
                },
            },
            "plugins": {"recursive": {"enabled": False}},
            "default_destroyed_host_persisted_seconds": 60 * 60 * 24 * 30,
        }
    )
    # Re-signin with identical data is a no-op.
    assert (
        set_imbue_cloud_provider_for_account(
            "alice@example.com",
            connector_url=_CONNECTOR_URL,
            root=_ROOT,
        )
        is False
    )
    assert unset_imbue_cloud_provider_for_account("alice@example.com", root=_ROOT) is True
    assert "imbue_cloud_alice-example-com" not in _parsed(settings_path)["providers"]
    assert unset_imbue_cloud_provider_for_account("alice@example.com", root=_ROOT) is False


def _write_accounts_index(emails: list[str]) -> None:
    """Write the plugin's accounts.json under the (HOME-stubbed) active profile dir."""
    sessions_dir = (
        mngr_host_dir_for(_ROOT_NAME) / "profiles" / "testprofile" / "providers" / "imbue_cloud" / "sessions"
    )
    sessions_dir.mkdir(parents=True, exist_ok=True)
    entries = [{"email": email, "user_id": f"user-{idx}"} for idx, email in enumerate(emails)]
    (sessions_dir / "accounts.json").write_text(json.dumps({"entries": entries}))


def test_reconcile_repairs_missing_block_without_re_enabling_disabled_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    for email in ("alice@example.com", "bob@example.com"):
        set_imbue_cloud_provider_for_account(email, connector_url=_CONNECTOR_URL, root=_ROOT)
    _write_accounts_index(["alice@example.com", "bob@example.com"])
    # Simulate drift: alice's block disabled via the panel, bob's block lost.
    set_provider_is_enabled("imbue_cloud_alice-example-com", False, root=_ROOT)
    unset_imbue_cloud_provider_for_account("bob@example.com", root=_ROOT)

    assert reconcile_imbue_cloud_providers_from_sessions(_CONNECTOR_URL, root=_ROOT) is True
    providers = _parsed(settings_path)["providers"]
    assert providers["imbue_cloud_alice-example-com"]["is_enabled"] is False
    assert providers["imbue_cloud_bob-example-com"]["backend"] == "imbue_cloud"
    # A second reconcile over the now-converged state changes nothing.
    assert reconcile_imbue_cloud_providers_from_sessions(_CONNECTOR_URL, root=_ROOT) is False


def test_byok_cloud_account_lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    aws_name = set_cloud_account_provider(
        "Work AWS",
        "aws",
        {"aws_access_key_id": "AKIA1234EXAMPLE9", "aws_secret_access_key": "secret", "aws_session_token": ""},
        "us-west-2",
        root=_ROOT,
    )
    gcp_name = set_cloud_account_provider(
        "gcp lab",
        "gcp",
        {"service_account_key_json": json.dumps({"client_email": "svc@proj.iam.gserviceaccount.com"})},
        "us-west1-a",
        root=_ROOT,
    )
    azure_name = set_cloud_account_provider(
        "azure lab",
        "azure",
        {"client_id": "11111111-2222-3333-4444-555555555555", "tenant_id": "tenant", "client_secret": "secret"},
        "eastus2",
        root=_ROOT,
    )
    assert (aws_name, gcp_name, azure_name) == snapshot(
        ("byok-aws-work-aws", "byok-gcp-gcp-lab", "byok-azure-azure-lab")
    )
    providers = _parsed(settings_path)["providers"]
    assert {name: block for name, block in providers.items() if name.startswith("byok-")} == snapshot(
        {
            "byok-aws-work-aws": {
                "backend": "aws",
                "host_dir": "/home/user/.mngr",
                "volume_home_path": "/home/user",
                "host_log_dir": "/var/log/mngr",
                "default_region": "us-west-2",
                "default_instance_type": "t3.large",
                "install_gvisor_runtime": True,
                "docker_runtime": "runsc",
                "default_start_args": ["--tmpfs", "/run"],
                "aws_access_key_id": "AKIA1234EXAMPLE9",
                "aws_secret_access_key": "secret",
            },
            "byok-gcp-gcp-lab": {
                "backend": "gcp",
                "host_dir": "/home/user/.mngr",
                "volume_home_path": "/home/user",
                "host_log_dir": "/var/log/mngr",
                "default_zone": "us-west1-a",
                "default_machine_type": "e2-standard-2",
                "service_account_key_json": '{"client_email": "svc@proj.iam.gserviceaccount.com"}',
            },
            "byok-azure-azure-lab": {
                "backend": "azure",
                "host_dir": "/home/user/.mngr",
                "volume_home_path": "/home/user",
                "host_log_dir": "/var/log/mngr",
                "default_region": "eastus2",
                "default_vm_size": "Standard_B2ms",
                "resource_group": "byok-azure-azure-lab-eastus2",
                "client_id": "11111111-2222-3333-4444-555555555555",
                "tenant_id": "tenant",
                "client_secret": "secret",
            },
        }
    )
    assert [account.model_dump() for account in list_cloud_account_providers(root=_ROOT)] == snapshot(
        [
            {
                "name": "byok-aws-work-aws",
                "alias": "work-aws",
                "backend": "aws",
                "region": "us-west-2",
                "identifier": "AKIA…PLE9",
            },
            {
                "name": "byok-azure-azure-lab",
                "alias": "azure-lab",
                "backend": "azure",
                "region": "eastus2",
                "identifier": "11111111-2222-3333-4444-555555555555",
            },
            {
                "name": "byok-gcp-gcp-lab",
                "alias": "gcp-lab",
                "backend": "gcp",
                "region": "us-west1-a",
                "identifier": "svc@proj.iam.gserviceaccount.com",
            },
        ]
    )
    # BYOK accounts survive the boot reconciler (the byok- prefix is exactly
    # what keeps them out of the ambient aws-* legacy cleanup).
    assert ensure_mngr_settings(_ROOT) is False
    assert "byok-aws-work-aws" in _parsed(settings_path)["providers"]

    assert delete_cloud_account_provider(aws_name, root=_ROOT) is True
    assert aws_name not in _parsed(settings_path)["providers"]
    assert delete_cloud_account_provider(aws_name, root=_ROOT) is False
    # Non-byok blocks are never deletable through this path.
    assert delete_cloud_account_provider("modal", root=_ROOT) is False
