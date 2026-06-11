"""Tests for GCP provider backend registration."""

import pytest
from google.auth.credentials import AnonymousCredentials
from google.auth.credentials import Credentials

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_gcp.backend import GCP_BACKEND_NAME
from imbue.mngr_gcp.backend import GcpProvider
from imbue.mngr_gcp.backend import GcpProviderBackend
from imbue.mngr_gcp.backend import ParsedGcpBuildOptions
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.config import GcpProviderConfig


class _StubAdcConfig(GcpProviderConfig):
    """GcpProviderConfig with ADC resolution stubbed for deterministic tests.

    ``build_provider_instance`` resolves credentials and the fallback project via
    ``get_credentials_and_resolved_project``, which calls ``google.auth.default()``.
    Stubbing it here keeps these tests independent of whatever gcloud / ADC state
    the test host happens to have configured.
    """

    stub_has_credentials: bool = True
    stub_resolved_project: str | None = None

    def get_credentials_and_resolved_project(self) -> tuple[Credentials, str | None]:
        if not self.stub_has_credentials:
            raise ValueError("GCP Application Default Credentials not configured (stub).")
        return AnonymousCredentials(), self.stub_resolved_project


def test_backend_name_and_config_class() -> None:
    assert GcpProviderBackend.get_name() == GCP_BACKEND_NAME
    assert GcpProviderBackend.get_config_class() is GcpProviderConfig


def test_backend_build_args_help_mentions_gcp_specific_args() -> None:
    """The build-args help is the only user-facing surface that describes
    GCE-specific build-arg overrides. It must mention the GCP-prefixed flags and
    call out that placement is a zone for GCP.
    """
    help_text = GcpProviderBackend.get_build_args_help()
    assert "GCE-specific" in help_text
    assert "--gcp-zone=ZONE" in help_text
    assert "--gcp-machine-type=TYPE" in help_text
    assert "zonal" in help_text
    # Document the per-host-image escape hatch is intentionally absent.
    assert "image" in help_text and "default_source_image" in help_text


def test_build_provider_instance_raises_provider_unavailable_without_credentials(
    temp_mngr_ctx: MngrContext,
) -> None:
    """No resolvable ADC surfaces as ProviderUnavailableError.

    Credentials failing means we never reached GCP, so the state is *unknown*
    (there may be running hosts we cannot see). ProviderUnavailableError -- not
    ProviderEmptyError -- is the correct signal: the shared discovery path
    surfaces it to the user instead of silently dropping the provider.
    """
    config = _StubAdcConfig(stub_has_credentials=False)
    with pytest.raises(ProviderUnavailableError):
        GcpProviderBackend.build_provider_instance(ProviderInstanceName("gcp-test"), config, temp_mngr_ctx)


def test_build_provider_instance_raises_provider_unavailable_without_project_anywhere(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Credentials but no project (neither configured nor ADC-resolved) -> unavailable.

    Without a project we cannot enumerate the provider's hosts, so its state is
    unknown and it must be surfaced as unavailable rather than half-constructed.
    """
    config = _StubAdcConfig(stub_has_credentials=True, stub_resolved_project=None)
    with pytest.raises(ProviderUnavailableError):
        GcpProviderBackend.build_provider_instance(ProviderInstanceName("gcp-test"), config, temp_mngr_ctx)


def test_build_provider_instance_falls_back_to_adc_resolved_project(
    temp_mngr_ctx: MngrContext,
) -> None:
    """With no configured project_id, the ADC-resolved project is used.

    This is the gcloud-default fallback: a user who ran `gcloud config set
    project` (or set GOOGLE_CLOUD_PROJECT) can create without pinning project_id
    in the mngr config.
    """
    config = _StubAdcConfig(stub_has_credentials=True, stub_resolved_project="adc-resolved-project")
    provider = GcpProviderBackend.build_provider_instance(ProviderInstanceName("gcp-test"), config, temp_mngr_ctx)
    assert isinstance(provider, GcpProvider)
    assert provider.gcp_client.project_id == "adc-resolved-project"


def test_build_provider_instance_prefers_configured_project_over_adc(
    temp_mngr_ctx: MngrContext,
) -> None:
    """An explicit project_id wins over whatever ADC resolved."""
    config = _StubAdcConfig(
        project_id="explicit-project",
        stub_has_credentials=True,
        stub_resolved_project="adc-resolved-project",
    )
    provider = GcpProviderBackend.build_provider_instance(ProviderInstanceName("gcp-test"), config, temp_mngr_ctx)
    assert isinstance(provider, GcpProvider)
    assert provider.gcp_client.project_id == "explicit-project"


class _FirewallStubClient(GcpVpsClient):
    """GcpVpsClient with firewall resolution stubbed, for hermetic create-hook tests.

    The real ``resolve_firewall`` makes a GCE API call. The pre-create hook now
    invokes it, so tests that exercise the hook stub it: ``resolve_firewall``
    returns the target tag, or (when ``stub_firewall_missing``) raises the same
    ``mngr gcp prepare`` MngrError the real method raises on a 404.
    """

    stub_firewall_missing: bool = False

    def resolve_firewall(self) -> str:
        if self.stub_firewall_missing:
            raise MngrError(
                f"GCP firewall rule {self.firewall_name!r} does not exist in project "
                f"{self.project_id!r}. Run `mngr gcp prepare --project {self.project_id}` once to create it."
            )
        return self.firewall_target_tag


def _build_provider(
    mngr_ctx: MngrContext, *, auto_shutdown_minutes: int | None, firewall_missing: bool = False
) -> GcpProvider:
    """Construct a GcpProvider with the given auto-shutdown and firewall settings.

    Uses anonymous credentials, a placeholder project, and a firewall-stubbed
    client: the create-hook and build-args tests that use this helper never make
    a real GCE API call.
    """
    config = GcpProviderConfig(
        backend=GCP_BACKEND_NAME,
        project_id="test-project",
        auto_shutdown_minutes=auto_shutdown_minutes,
    )
    client = _FirewallStubClient(
        credentials=AnonymousCredentials(),
        project_id="test-project",
        zone=config.default_zone,
        image=config.default_source_image,
        auto_shutdown_minutes=auto_shutdown_minutes,
        stub_firewall_missing=firewall_missing,
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
    # No exception raised (auto_shutdown set, firewall present).
    provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_raises_when_zero(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=0)
    with pytest.raises(MngrError, match="auto_shutdown_minutes"):
        provider._validate_provider_args_for_create()


def test_validate_provider_args_requires_firewall_rule(temp_mngr_ctx: MngrContext) -> None:
    """The pre-create hook fails fast with the `mngr gcp prepare` pointer when the rule is missing.

    This is the onboarding path: a first-time user who has not run prepare must
    get the actionable message before any provider write, not buried under a
    "Host creation failed, attempting cleanup..." line mid-create.
    """
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60, firewall_missing=True)
    with pytest.raises(MngrError, match="mngr gcp prepare"):
        provider._validate_provider_args_for_create()


# =============================================================================
# GCP build-args parser (--gcp-zone, --gcp-machine-type, --git-depth)
# =============================================================================


def test_parse_build_args_uses_defaults_when_none(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    parsed = provider._parse_build_args(None)
    # region holds the zone for GCP (base threads it to create_instance).
    assert parsed.region == "us-west1-a"
    assert parsed.plan == "e2-small"
    assert parsed.spot is False
    assert parsed.git_depth is None
    assert parsed.docker_build_args == ()


def test_parse_build_args_extracts_gcp_knobs_plus_docker_passthrough(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    parsed = provider._parse_build_args(
        [
            "--gcp-zone=us-west1-b",
            "--gcp-machine-type=e2-medium",
            "--gcp-spot",
            "--git-depth=1",
            "--file=Dockerfile",
            ".",
        ]
    )
    assert isinstance(parsed, ParsedGcpBuildOptions)
    assert parsed.region == "us-west1-b"
    assert parsed.plan == "e2-medium"
    assert parsed.spot is True
    assert parsed.git_depth == 1
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_rejects_gcp_spot_with_value(temp_mngr_ctx: MngrContext) -> None:
    """``--gcp-spot`` is presence-only; passing a value (e.g. ``--gcp-spot=true``) raises."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    with pytest.raises(MngrError, match="presence-only flag"):
        provider._parse_build_args(["--gcp-spot=true"])


def test_parse_build_args_rejects_unknown_gcp_flag(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    with pytest.raises(MngrError, match="Unknown gcp build arg"):
        provider._parse_build_args(["--gcp-bogus=foo"])


def test_parse_build_args_rejects_dropped_vps_prefix(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    with pytest.raises(MngrError, match="no longer supported"):
        provider._parse_build_args(["--vps-region=us-west1-a"])
