from pathlib import Path

import pytest

from imbue.minds.desktop_client.connections.local_profiles import LocalCredentialProfile
from imbue.minds.desktop_client.connections.local_profiles import list_aws_profiles
from imbue.minds.desktop_client.connections.local_profiles import list_azure_cli_subscriptions
from imbue.minds.desktop_client.connections.local_profiles import list_local_credential_profiles
from imbue.minds.desktop_client.connections.local_profiles import list_modal_profiles
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend


def test_aws_profiles_merge_both_files_and_mark_the_ones_mngr_cannot_use(tmp_path: Path) -> None:
    (tmp_path / "config").write_text(
        "[default]\nregion = us-east-1\n\n[profile work]\nregion = eu-west-1\n\n"
        "[profile guarded]\nmfa_serial = arn:aws:iam::1:mfa/me\n\n[not-a-profile]\nregion = x\n"
    )
    (tmp_path / "credentials").write_text(
        "[work]\naws_access_key_id = A\naws_secret_access_key = S\n\n[keys-only]\naws_access_key_id = B\naws_secret_access_key = T\n"
    )

    profiles = list_aws_profiles(tmp_path / "config", tmp_path / "credentials")

    assert profiles == [
        LocalCredentialProfile(name="default", label="default", region="us-east-1"),
        LocalCredentialProfile(
            name="guarded",
            label="guarded",
            unusable_reason="needs an MFA code on every use, which the app cannot enter",
        ),
        LocalCredentialProfile(name="keys-only", label="keys-only"),
        LocalCredentialProfile(name="work", label="work", region="eu-west-1"),
    ]


def test_aws_profiles_are_empty_without_the_files(tmp_path: Path) -> None:
    assert list_aws_profiles(tmp_path / "config", tmp_path / "credentials") == []


def test_modal_profiles_list_only_sections_holding_a_token_pair(tmp_path: Path) -> None:
    config_path = tmp_path / "modal.toml"
    config_path.write_text(
        '[default]\ntoken_id = "ak-d"\ntoken_secret = "as-d"\nactive = true\n\n'
        '[work]\ntoken_id = "ak-w"\ntoken_secret = "as-w"\n\n[broken]\ntoken_id = "ak-b"\n'
    )

    assert [profile.name for profile in list_modal_profiles(config_path)] == ["default", "work"]
    assert list_modal_profiles(tmp_path / "missing.toml") == []


def test_azure_subscriptions_come_from_the_az_profile_default_first_and_enabled_only(tmp_path: Path) -> None:
    profile_path = tmp_path / "azureProfile.json"
    profile_path.write_bytes(
        b"\xef\xbb\xbf"
        + (
            '{"subscriptions": ['
            '{"id": "sub-2", "name": "Second", "tenantId": "tenant-1", "isDefault": false, "state": "Enabled"},'
            '{"id": "sub-1", "name": "First", "tenantId": "tenant-1", "isDefault": true, "state": "Enabled"},'
            '{"id": "sub-3", "name": "Gone", "tenantId": "tenant-2", "isDefault": false, "state": "Disabled"}'
            "]}"
        ).encode()
    )

    profiles = list_azure_cli_subscriptions(profile_path)

    assert profiles == [
        LocalCredentialProfile(name="sub-1", label="First (sub-1)", tenant_id="tenant-1", subscription_id="sub-1"),
        LocalCredentialProfile(name="sub-2", label="Second (sub-2)", tenant_id="tenant-1", subscription_id="sub-2"),
    ]
    assert list_azure_cli_subscriptions(tmp_path / "missing.json") == []


def test_listing_honors_each_tools_file_location_variable_and_gcp_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "aws-config").write_text("[profile work]\nregion = eu-west-1\n")
    (tmp_path / "modal.toml").write_text('[work]\ntoken_id = "ak"\ntoken_secret = "as"\n')
    (tmp_path / "azure").mkdir()
    (tmp_path / "azure" / "azureProfile.json").write_text(
        '{"subscriptions": [{"id": "sub-1", "name": "Only", "tenantId": "t", "isDefault": true}]}'
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-credentials"))
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(tmp_path / "modal.toml"))
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path / "azure"))

    assert [p.name for p in list_local_credential_profiles(ConnectionBackend.AWS)] == ["work"]
    assert [p.name for p in list_local_credential_profiles(ConnectionBackend.MODAL)] == ["work"]
    assert [p.name for p in list_local_credential_profiles(ConnectionBackend.AZURE)] == ["sub-1"]
    assert list_local_credential_profiles(ConnectionBackend.GCP) == []
