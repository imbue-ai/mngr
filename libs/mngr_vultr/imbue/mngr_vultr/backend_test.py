"""Tests for Vultr provider backend registration and instance construction."""

import json
from typing import Any

import pytest
import requests
from pydantic import SecretStr

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig
from imbue.mngr_vultr.backend import VULTR_BACKEND_NAME
from imbue.mngr_vultr.backend import VultrProvider
from imbue.mngr_vultr.backend import VultrProviderBackend
from imbue.mngr_vultr.backend import register_provider_backend
from imbue.mngr_vultr.client import VultrVpsClient
from imbue.mngr_vultr.config import VultrProviderConfig


def _client_returning_instances(instances: list[dict[str, Any]], api_key: str = "test-key") -> VultrVpsClient:
    """Build a VultrVpsClient whose list_instances() yields the given canned instances."""

    def _transport(**kwargs: Any) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"instances": instances}).encode()
        response.headers["content-type"] = "application/json"
        return response

    return VultrVpsClient(api_key=SecretStr(api_key), request_func=_transport)


def _vultr_provider(client: VultrVpsClient, mngr_ctx: MngrContext) -> VultrProvider:
    config = VultrProviderConfig(api_key=client.api_key)
    return VultrProvider(
        name=ProviderInstanceName("vultr"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        vultr_client=client,
        vultr_config=config,
    )


def test_backend_name() -> None:
    assert VultrProviderBackend.get_name() == ProviderBackendName("vultr")


def test_backend_name_constant() -> None:
    assert VULTR_BACKEND_NAME == ProviderBackendName("vultr")


def test_backend_description() -> None:
    desc = VultrProviderBackend.get_description()
    assert "Vultr" in desc
    assert "Docker" in desc


def test_backend_config_class() -> None:
    config_cls = VultrProviderBackend.get_config_class()
    assert config_cls is VultrProviderConfig


def test_backend_build_args_help() -> None:
    help_text = VultrProviderBackend.get_build_args_help()
    assert "--vps-region" in help_text
    assert "--vps-plan" in help_text
    assert "--vps-os" in help_text


def test_backend_start_args_help() -> None:
    help_text = VultrProviderBackend.get_start_args_help()
    assert "docker run" in help_text


def test_register_provider_backend_returns_tuple() -> None:
    result = register_provider_backend()
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert result[0] is VultrProviderBackend
    assert result[1] is VultrProviderConfig


def test_build_provider_instance_rejects_wrong_config_type(temp_mngr_ctx: MngrContext) -> None:
    wrong_config = VpsDockerProviderConfig(backend=ProviderBackendName("docker"))
    with pytest.raises(MngrError, match="Expected VultrProviderConfig"):
        VultrProviderBackend.build_provider_instance(ProviderInstanceName("vultr"), wrong_config, temp_mngr_ctx)


def test_build_provider_instance_uses_configured_api_key(temp_mngr_ctx: MngrContext) -> None:
    config = VultrProviderConfig(api_key=SecretStr("configured-key"))
    provider = VultrProviderBackend.build_provider_instance(ProviderInstanceName("vultr"), config, temp_mngr_ctx)
    assert isinstance(provider, VultrProvider)
    assert provider.vultr_client.api_key.get_secret_value() == "configured-key"


def test_build_provider_instance_falls_back_to_empty_key_when_unset(
    temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no configured key and no env var, the backend must build a
    # discoverable-but-inert provider with an empty key rather than raising.
    monkeypatch.delenv("VULTR_API_KEY", raising=False)
    config = VultrProviderConfig()
    provider = VultrProviderBackend.build_provider_instance(ProviderInstanceName("vultr"), config, temp_mngr_ctx)
    assert isinstance(provider, VultrProvider)
    assert provider.vultr_client.api_key.get_secret_value() == ""


def test_list_provider_vps_hostnames_returns_ips_of_matching_tagged_instances(
    temp_mngr_ctx: MngrContext,
) -> None:
    client = _client_returning_instances(
        [
            {"tags": ["mngr-provider=vultr"], "main_ip": "1.2.3.4"},
            {"tags": ["mngr-provider=other"], "main_ip": "5.6.7.8"},
            {"tags": [], "main_ip": "9.9.9.9"},
        ]
    )
    provider = _vultr_provider(client, temp_mngr_ctx)
    assert provider._list_provider_vps_hostnames() == ["1.2.3.4"]


def test_list_provider_vps_hostnames_skips_unprovisioned_placeholder_ips(
    temp_mngr_ctx: MngrContext,
) -> None:
    client = _client_returning_instances(
        [
            {"tags": ["mngr-provider=vultr"], "main_ip": "0.0.0.0"},
            {"tags": ["mngr-provider=vultr"], "main_ip": ""},
        ]
    )
    provider = _vultr_provider(client, temp_mngr_ctx)
    assert provider._list_provider_vps_hostnames() == []


def test_list_provider_vps_hostnames_returns_empty_when_api_key_missing(
    temp_mngr_ctx: MngrContext,
) -> None:
    client = _client_returning_instances([{"tags": ["mngr-provider=vultr"], "main_ip": "1.2.3.4"}], api_key="")
    provider = _vultr_provider(client, temp_mngr_ctx)
    assert provider._list_provider_vps_hostnames() == []
