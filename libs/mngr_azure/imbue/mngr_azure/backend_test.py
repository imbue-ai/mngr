from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_azure.backend import AzureProvider
from imbue.mngr_azure.backend import AzureProviderBackend
from imbue.mngr_azure.backend import ParsedAzureBuildOptions
from imbue.mngr_azure.config import AzureProviderConfig


def _build_provider(mngr_ctx: MngrContext, *, auto_shutdown_minutes: int | None) -> AzureProvider:
    """Construct an AzureProvider via the backend with the given auto-shutdown setting.

    The credential is a real ``DefaultAzureCredential`` (constructed lazily, no
    network call), and the guard/parse hooks under test never reach the SDK.
    """
    config = AzureProviderConfig(subscription_id="sub-123", auto_shutdown_minutes=auto_shutdown_minutes)
    provider = AzureProviderBackend.build_provider_instance(
        name=ProviderInstanceName("azure"), config=config, mngr_ctx=mngr_ctx
    )
    assert isinstance(provider, AzureProvider)
    return provider


def test_backend_name_and_config_class() -> None:
    assert str(AzureProviderBackend.get_name()) == "azure"
    assert AzureProviderBackend.get_config_class() is AzureProviderConfig


def test_backend_build_args_help_mentions_azure_specific_args() -> None:
    help_text = AzureProviderBackend.get_build_args_help()
    assert "--azure-region=" in help_text
    assert "--azure-vm-size=" in help_text
    assert "--azure-spot" in help_text


def test_build_provider_instance_raises_provider_unavailable_without_subscription(
    temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An unresolvable subscription means Azure was never reached, so its state is
    # unknown: the backend must raise ProviderUnavailableError (warned by read
    # paths), NOT ProviderEmptyError (silently skipped) -- otherwise a transient
    # read failure would silently drop azure agents from `mngr list`.
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    # Isolate AZURE_CONFIG_DIR (the conftest autouse fixture pins it at the real
    # ~/.azure) so the az-default-subscription fallback resolves nothing here.
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    config = AzureProviderConfig()
    with pytest.raises(ProviderUnavailableError):
        AzureProviderBackend.build_provider_instance(
            name=ProviderInstanceName("azure"), config=config, mngr_ctx=temp_mngr_ctx
        )


def test_unavailable_error_help_text_is_azure_curated_not_start_docker(
    temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The unresolvable-provider error must give cloud-auth guidance, not 'start Docker'.

    The generic ProviderUnavailableError help text tells the user to start Docker,
    which is wrong advice for an Azure subscription/credential failure.
    """
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    config = AzureProviderConfig()
    with pytest.raises(ProviderUnavailableError) as exc_info:
        AzureProviderBackend.build_provider_instance(
            name=ProviderInstanceName("azure"), config=config, mngr_ctx=temp_mngr_ctx
        )
    help_text = exc_info.value.user_help_text
    assert help_text is not None
    assert "Docker" not in help_text
    assert "AZURE_SUBSCRIPTION_ID" in help_text
    assert "az login" in help_text
    assert "mngr azure prepare" in help_text


def test_validate_provider_args_under_pytest_raises_when_unset(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=None)
    with pytest.raises(MngrError, match="auto_shutdown_minutes"):
        provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_accepts_positive(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    # Should not raise.
    provider._validate_provider_args_for_create()


def test_parse_build_args_uses_defaults_when_none(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    parsed = provider._parse_build_args(None)
    assert isinstance(parsed, ParsedAzureBuildOptions)
    assert parsed.region == "westus"
    assert parsed.plan == "Standard_B2s"
    assert parsed.spot is False
    assert parsed.git_depth is None


def test_parse_build_args_extracts_azure_knobs_plus_docker_passthrough(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    parsed = provider._parse_build_args(
        ["--azure-region=eastus", "--azure-vm-size=Standard_D2s_v5", "--azure-spot", "--file=Dockerfile", "."]
    )
    assert parsed.region == "eastus"
    assert parsed.plan == "Standard_D2s_v5"
    assert parsed.spot is True
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_rejects_unknown_azure_flag(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    with pytest.raises(MngrError, match="Unknown azure build arg"):
        provider._parse_build_args(["--azure-bogus=1"])
