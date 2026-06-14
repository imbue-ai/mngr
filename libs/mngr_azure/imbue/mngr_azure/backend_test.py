from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_azure.backend import AzureProvider
from imbue.mngr_azure.backend import AzureProviderBackend
from imbue.mngr_azure.backend import ParsedAzureBuildOptions
from imbue.mngr_azure.client import AzureVpsClient
from imbue.mngr_azure.config import AzureProviderConfig


class _SubnetStubClient(AzureVpsClient):
    """AzureVpsClient with subnet resolution stubbed, for hermetic create-hook tests.

    The real ``resolve_subnet_id`` makes an Azure network API call. The pre-create
    hook now invokes it, so tests that exercise the hook stub it: it returns a
    placeholder id, or (when ``stub_subnet_missing``) raises the same
    ``mngr azure prepare`` MngrError the real method raises on a 404.
    """

    stub_subnet_missing: bool = False

    def resolve_subnet_id(self) -> str:
        if self.stub_subnet_missing:
            raise MngrError(
                f"Azure subnet {self.subnet_name!r} (vnet {self.vnet_name!r}, resource group "
                f"{self.resource_group!r}) does not exist in region {self.region!r}. "
                "Run `mngr azure prepare` once to create the resource group / vnet / subnet / NSG, "
                "then retry the create."
            )
        return f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}/subnets/{self.subnet_name}"


def _build_provider(
    mngr_ctx: MngrContext, *, auto_shutdown_seconds: int | None, subnet_missing: bool = False
) -> AzureProvider:
    """Construct an AzureProvider with the given auto-shutdown and subnet settings.

    Uses a placeholder credential and a subnet-stubbed client: the create-hook and
    build-args tests that use this helper never make a real Azure API call.
    """
    config = AzureProviderConfig(subscription_id="sub-123", auto_shutdown_seconds=auto_shutdown_seconds)
    client = _SubnetStubClient(
        credential=object(),
        subscription_id="sub-123",
        region=config.default_region,
        resource_group=config.resource_group,
        vnet_name=config.vnet_name,
        subnet_name=config.subnet_name,
        nsg_name=config.nsg_name,
        stub_subnet_missing=subnet_missing,
    )
    return AzureProvider(
        name=ProviderInstanceName("azure-test"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        azure_client=client,
        azure_config=config,
    )


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
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=None)
    with pytest.raises(MngrError, match="auto_shutdown_seconds"):
        provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_accepts_positive(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=3600)
    # Should not raise: auto_shutdown is set and the subnet pre-flight resolves
    # (the stub client reports the prepared subnet present).
    provider._validate_provider_args_for_create()


def test_validate_provider_args_raises_when_subnet_missing(temp_mngr_ctx: MngrContext) -> None:
    """The read-only subnet pre-flight fires before any VM write when prepare wasn't run.

    A first-time user who skipped ``mngr azure prepare`` should get the clean
    prepare-pointer error from the pre-create hook, not mid-create under a
    "Host creation failed, attempting cleanup..." line.
    """
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=3600, subnet_missing=True)
    with pytest.raises(MngrError, match="mngr azure prepare"):
        provider._validate_provider_args_for_create()


def test_parse_build_args_uses_defaults_when_none(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=3600)
    parsed = provider._parse_build_args(None)
    assert isinstance(parsed, ParsedAzureBuildOptions)
    assert parsed.region == "westus"
    assert parsed.plan == "Standard_B2s"
    assert parsed.spot is False
    assert parsed.git_depth is None


def test_parse_build_args_extracts_azure_knobs_plus_docker_passthrough(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=3600)
    parsed = provider._parse_build_args(
        ["--azure-region=eastus", "--azure-vm-size=Standard_D2s_v5", "--azure-spot", "--file=Dockerfile", "."]
    )
    assert parsed.region == "eastus"
    assert parsed.plan == "Standard_D2s_v5"
    assert parsed.spot is True
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_rejects_unknown_azure_flag(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=3600)
    with pytest.raises(MngrError, match="Unknown azure build arg"):
        provider._parse_build_args(["--azure-bogus=1"])
