"""Tests for the shared cloud-provider operator-CLI helpers.

Exercises ``resolve_provider_config`` (the {configured / wrong-backend /
missing} contract that the AWS / Azure / GCP / OVH CLIs each delegate to) and
``refuse_if_managed_resources_exist`` (the shared cleanup-refusal guard, whose
unified ``ManagedResourcesExistError`` renders identically across providers)
against a small concrete provider config, without depending on any one cloud
plugin.
"""

import pytest
from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.config import LocalProviderConfig
from imbue.mngr_vps.cli_helpers import refuse_if_managed_resources_exist
from imbue.mngr_vps.cli_helpers import resolve_provider_config
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.errors import ManagedResourcesExistError

_TEST_BACKEND = ProviderBackendName("vps-cli-helpers-test")


class _FakeCloudConfig(VpsProviderConfig):
    """A concrete provider config with a defaulted ``backend`` (so it is no-arg constructible)."""

    backend: ProviderBackendName = Field(default=_TEST_BACKEND)
    marker: str = Field(default="default")


def _ctx_with_provider(temp_mngr_ctx: MngrContext, name: str, config: ProviderInstanceConfig) -> MngrContext:
    provider_name = ProviderInstanceName(name)
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().providers, {provider_name: config})
    )
    return temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, new_config))


def _resolve(ctx: MngrContext, name: str) -> _FakeCloudConfig:
    return resolve_provider_config(
        ctx,
        name,
        config_cls=_FakeCloudConfig,
        default_factory=_FakeCloudConfig,
        cloud_label="a fake-cloud backend",
        override_hint="Point --provider at a fake-cloud block.",
    )


def test_resolve_returns_configured_block_silently(temp_mngr_ctx: MngrContext, log_warnings: list[str]) -> None:
    """A matching configured block is returned verbatim, with no warning."""
    user_config = _FakeCloudConfig(backend=_TEST_BACKEND, marker="user-value")
    ctx = _ctx_with_provider(temp_mngr_ctx, "fake-prod", user_config)

    resolved = _resolve(ctx, "fake-prod")

    assert resolved.marker == "user-value"
    assert log_warnings == [], f"happy path must be silent, got {log_warnings!r}"


def test_resolve_falls_back_to_defaults_when_missing(temp_mngr_ctx: MngrContext, log_warnings: list[str]) -> None:
    """A missing block falls back to ``default_factory()`` silently (expected first-run shape)."""
    resolved = _resolve(temp_mngr_ctx, "fake-does-not-exist")

    assert resolved == _FakeCloudConfig()
    assert log_warnings == [], f"missing-block fallback must be silent, got {log_warnings!r}"


def test_resolve_warns_and_falls_back_on_wrong_backend(temp_mngr_ctx: MngrContext, log_warnings: list[str]) -> None:
    """A block pointed at a different backend falls back to defaults and warns."""
    ctx = _ctx_with_provider(temp_mngr_ctx, "fake", LocalProviderConfig())

    resolved = _resolve(ctx, "fake")

    assert resolved == _FakeCloudConfig()
    assert len(log_warnings) == 1, f"expected exactly one warning, got {log_warnings!r}"
    assert "'fake'" in log_warnings[0]
    assert "LocalProviderConfig" in log_warnings[0]
    assert "Point --provider at a fake-cloud block." in log_warnings[0]


def test_refuse_is_noop_when_no_resources_exist() -> None:
    """An empty resource list is a no-op (the delete path proceeds)."""
    refuse_if_managed_resources_exist(
        [],
        summary="",
        resource_noun="instance",
        scope_description="region us-east-1",
        cleanup_command="mngr fake cleanup",
    )


def test_refuse_raises_unified_error_naming_blockers() -> None:
    """A non-empty list raises ``ManagedResourcesExistError`` naming the blockers and the re-run command."""
    with pytest.raises(ManagedResourcesExistError) as exc_info:
        refuse_if_managed_resources_exist(
            ["i-abc", "i-def"],
            summary="i-abc (running), i-def (stopped)",
            resource_noun="instance",
            scope_description="region us-east-1",
            cleanup_command="mngr fake cleanup",
        )
    message = str(exc_info.value)
    assert "Refusing to clean up region us-east-1" in message
    assert "2 mngr-managed instance(s)" in message
    assert "i-abc (running), i-def (stopped)" in message
    assert "mngr fake cleanup" in message
