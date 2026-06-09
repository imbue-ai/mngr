"""Tests for GCP provider backend registration."""

import pytest
from google.auth.credentials import AnonymousCredentials

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_gcp.backend import GCP_BACKEND_NAME
from imbue.mngr_gcp.backend import GcpProvider
from imbue.mngr_gcp.backend import GcpProviderBackend
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.config import GcpProviderConfig


def test_backend_name_and_config_class() -> None:
    assert GcpProviderBackend.get_name() == GCP_BACKEND_NAME
    assert GcpProviderBackend.get_config_class() is GcpProviderConfig


def test_backend_build_args_help_mentions_gcp_specific_args() -> None:
    """The build-args help is the only user-facing surface that describes
    GCE-specific build-arg overrides. It must mention the GCE-specific flags and
    call out that --vps-region is a zone for GCP.
    """
    help_text = GcpProviderBackend.get_build_args_help()
    assert "GCE-specific" in help_text
    assert "--vps-region=ZONE" in help_text
    assert "--vps-plan=TYPE" in help_text
    assert "zonal" in help_text
    # Document the per-host-image escape hatch is intentionally absent.
    assert "Image" in help_text and "default_image" in help_text


def test_build_provider_instance_raises_provider_empty_without_project(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Missing project_id surfaces as ProviderEmptyError so read paths skip GCP.

    ADC may be resolvable in the test environment, but project_id is empty by
    default, so build must fail with ProviderEmptyError (mirroring the AWS
    no-AMI / no-creds case).
    """
    config = GcpProviderConfig()
    with pytest.raises(ProviderEmptyError):
        GcpProviderBackend.build_provider_instance(ProviderInstanceName("gcp-test"), config, temp_mngr_ctx)


def test_build_provider_instance_rejects_wrong_config_type(temp_mngr_ctx: MngrContext) -> None:
    # A config that is not a GcpProviderConfig trips the isinstance guard before
    # any credential resolution.
    with pytest.raises(MngrError, match="Expected GcpProviderConfig"):
        GcpProviderBackend.build_provider_instance(
            ProviderInstanceName("gcp-test"),
            object(),  # type: ignore[arg-type]
            temp_mngr_ctx,
        )


def _build_provider(mngr_ctx: MngrContext, *, auto_shutdown_minutes: int | None) -> GcpProvider:
    """Construct a GcpProvider with the given auto-shutdown setting.

    Uses anonymous credentials and a placeholder project: this helper is only
    used by tests that exercise the pytest-detection guard, which fires before
    any GCE API call, so the credentials/project are never touched.
    """
    config = GcpProviderConfig(
        backend=GCP_BACKEND_NAME,
        project_id="test-project",
        auto_shutdown_minutes=auto_shutdown_minutes,
    )
    client = GcpVpsClient(
        credentials=AnonymousCredentials(),
        project_id="test-project",
        zone=config.default_zone,
        image=config.default_image,
        auto_shutdown_minutes=auto_shutdown_minutes,
    )
    return GcpProvider(
        name=ProviderInstanceName("gcp-test"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        gcp_client=client,
        gcp_config=config,
    )


def test_validate_provider_args_under_pytest_raises_when_unset(temp_mngr_ctx: MngrContext) -> None:
    """The pre-create hook fires when auto_shutdown_minutes is None (the config default).

    Without it, a release test would launch instances with no self-delete safety
    net. The hook must abort the launch before any GCE API call.
    """
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=None)
    with pytest.raises(MngrError, match="auto_shutdown_minutes"):
        provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_accepts_positive(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    # No exception raised.
    provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_raises_when_zero(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=0)
    with pytest.raises(MngrError, match="auto_shutdown_minutes"):
        provider._validate_provider_args_for_create()
