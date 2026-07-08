"""Tests for the ``mngr ovh`` CLI subcommands."""

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import ovh
import pluggy
import pytest
from click.testing import CliRunner

from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.config import LocalProviderConfig
from imbue.mngr_ovh.backend import OVH_BACKEND_NAME
from imbue.mngr_ovh.cli import _resolve_provider_config
from imbue.mngr_ovh.cli import ovh as ovh_group
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.config import OvhProviderConfig


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip OVH_* env vars so the test controls the (un)configured state."""
    for name in (
        "OVH_ENDPOINT",
        "OVH_APPLICATION_KEY",
        "OVH_APPLICATION_SECRET",
        "OVH_APP_KEY",
        "OVH_APP_SECRET",
        "OVH_CONSUMER_KEY",
        "OVH_CLIENT_ID",
        "OVH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def _patch_build_ovh_client(call_side_effect: Any) -> Any:
    """Patch ``build_ovh_client`` to return a fake-backed ``OvhVpsClient``.

    The fake ``ovh.Client.call`` dispatches through ``call_side_effect``,
    which lets each test script the exact /vps and /v2/iam/resource
    responses it wants without monkeypatching python-ovh itself.
    """
    raw_client = MagicMock(spec=ovh.Client)
    raw_client.call = MagicMock(side_effect=call_side_effect)
    client = OvhVpsClient(ovh_client=raw_client, subsidiary="US", task_poll_interval=0.0)
    return patch("imbue.mngr_ovh.cli.build_ovh_client", return_value=client)


def test_list_errors_clearly_when_unconfigured(
    clean_env: None,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    raw_client = MagicMock(spec=ovh.Client)
    raw_client.call = MagicMock(side_effect=AssertionError("no API call should fire when unconfigured"))
    placeholder = OvhVpsClient(ovh_client=raw_client, subsidiary="US", task_poll_interval=0.0, is_unconfigured=True)
    with patch("imbue.mngr_ovh.cli.build_ovh_client", return_value=placeholder):
        result = cli_runner.invoke(ovh_group, ["list"], obj=plugin_manager)
    assert result.exit_code != 0
    assert "OVH credentials not configured" in result.output


def test_list_with_no_vpses_prints_empty_message(
    clean_env: None,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path == "/vps":
            return []
        raise AssertionError(f"unexpected {method} {path}")

    with _patch_build_ovh_client(fake):
        result = cli_runner.invoke(ovh_group, ["list"], obj=plugin_manager)
    assert result.exit_code == 0, result.output
    assert "no OVH VPSes" in result.output


def test_list_default_hides_untagged_vpses(
    clean_env: None,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Without ``--all``, untagged VPSes are filtered out of the rendered table."""

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path == "/vps":
            return ["vps-untagged.vps.ovh.us"]
        if method == "GET" and path == "/v2/iam/resource?resourceType=vps":
            return []
        if method == "GET" and path.startswith("/vps/") and path.count("/") == 2:
            return {
                "state": "running",
                "model": {"name": "vps-2025-model1"},
                "zone": "Region OpenStack: os-us-east-va-vps-1",
                "name": "vps-untagged.vps.ovh.us",
                "displayName": "vps-untagged.vps.ovh.us",
            }
        if method == "GET" and path.endswith("/serviceInfos"):
            return {
                "renew": {"deleteAtExpiration": False},
                "expiration": "2026-06-15",
                "status": "ok",
            }
        raise AssertionError(f"unexpected {method} {path}")

    with _patch_build_ovh_client(fake):
        result = cli_runner.invoke(ovh_group, ["list"], obj=plugin_manager)
    assert result.exit_code == 0, result.output
    assert "no mngr-tagged OVH VPSes" in result.output
    assert "vps-untagged.vps.ovh.us" not in result.output


def test_list_renders_tagged_vps(
    clean_env: None,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path == "/vps":
            return ["vps-eec8860b.vps.ovh.us"]
        if method == "GET" and path == "/v2/iam/resource?resourceType=vps":
            return [
                {
                    "urn": "urn:v1:us:resource:vps:vps-eec8860b.vps.ovh.us",
                    "name": "vps-eec8860b.vps.ovh.us",
                    "displayName": "vps-eec8860b.vps.ovh.us",
                    "type": "vps",
                    "tags": {
                        "mngr-provider": "alice-ovh",
                        "mngr-host-id": "host-abc123",
                    },
                }
            ]
        if method == "GET" and path.startswith("/vps/") and path.count("/") == 2:
            return {
                "state": "running",
                "model": {"name": "vps-2025-model1"},
                "zone": "Region OpenStack: os-us-east-va-vps-1",
                "name": "vps-eec8860b.vps.ovh.us",
                "displayName": "vps-eec8860b.vps.ovh.us",
            }
        if method == "GET" and path.endswith("/serviceInfos"):
            return {
                "renew": {"deleteAtExpiration": True},
                "expiration": "2026-06-15",
                "status": "ok",
            }
        raise AssertionError(f"unexpected {method} {path}")

    with _patch_build_ovh_client(fake):
        result = cli_runner.invoke(ovh_group, ["list"], obj=plugin_manager)
    assert result.exit_code == 0, result.output
    out = result.output
    # Header
    assert "SERVICENAME" in out
    assert "MNGR-PROVIDER" in out
    # Row data
    assert "vps-eec8860b.vps.ovh.us" in out
    assert "vps-2025-model1" in out
    assert "running" in out
    assert "2026-06-15" in out
    assert "alice-ovh" in out
    assert "host-abc123" in out
    # Cancellation column should read "yes" since deleteAtExpiration=True
    assert "yes" in out


def test_list_command_help_is_reachable() -> None:
    result = CliRunner().invoke(ovh_group, ["list", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--all" in result.output


# =============================================================================
# Provider-config resolution (the bug-fix surface for `mngr ovh list`)
# =============================================================================
#
# Earlier versions of `list_command` built `OvhProviderConfig()` with class
# defaults unconditionally, so it always talked to the default OVH endpoint /
# subsidiary regardless of what the user pinned in `[providers.ovh]`. A user who
# configured a non-default `endpoint` / `ovh_subsidiary` (or credentials) in
# their provider block and ran `mngr ovh list` would inspect a different account
# than the runtime `mngr create --provider <name>` path uses.
# `_resolve_provider_config` fixes this by reading the user's resolved provider
# config off the `MngrContext`; these tests pin that behavior. Mirrors the AWS /
# GCP / Azure providers' identical fix.


def _temp_mngr_ctx_with_provider(temp_mngr_ctx: MngrContext, name: str, config: ProviderInstanceConfig) -> MngrContext:
    """Return ``temp_mngr_ctx`` with ``config`` registered under ``name`` in ``providers``."""
    provider_name = ProviderInstanceName(name)
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().providers, {provider_name: config})
    )
    return temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, new_config))


def test_resolve_provider_config_uses_user_provider_block(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """The happy path returns the configured ``OvhProviderConfig`` verbatim, silently.

    Pins the third leg of the three-case contract: configured OVH block ->
    return as-is, no warning. The two sibling tests cover the missing-block and
    non-OVH-block fallbacks (silent and warning respectively); pinning silence
    here too closes the {OVH / non-OVH / missing} x {warn / silent} matrix so a
    future regression that always-warns can't slip through.
    """
    # Use values that differ from OvhProviderConfig() class defaults (endpoint
    # 'ovh-us', subsidiary 'US', region 'US-EAST-VA') so the test proves the
    # configured block is returned rather than the class defaults.
    user_config = OvhProviderConfig(
        backend=OVH_BACKEND_NAME,
        endpoint="ovh-eu",
        ovh_subsidiary="FR",
        default_region="GRA",
    )
    ctx_with_provider = _temp_mngr_ctx_with_provider(temp_mngr_ctx, "ovh-eu", user_config)

    resolved = _resolve_provider_config(ctx_with_provider, "ovh-eu")

    assert resolved.endpoint == "ovh-eu"
    assert resolved.ovh_subsidiary == "FR"
    assert resolved.default_region == "GRA"
    assert log_warnings == [], f"happy path must be silent, got {log_warnings!r}"


def test_resolve_provider_config_falls_back_to_class_defaults_when_missing(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """When the named provider block doesn't exist, class defaults are used silently.

    ``mngr ovh list`` must work for users who haven't pinned a ``[providers.ovh]``
    block (credentials still come from env / ~/.ovh.conf), so the fallback is a
    feature not a bug -- and no warning is emitted because this is the expected
    shape (distinct from the wrong-type case, which does warn).
    """
    resolved = _resolve_provider_config(temp_mngr_ctx, "ovh-does-not-exist")

    assert resolved == OvhProviderConfig()
    assert log_warnings == [], f"missing-block fallback must be silent, got {log_warnings!r}"


def test_resolve_provider_config_falls_back_when_named_block_is_non_ovh(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """If the user pointed ``[providers.ovh]`` at a non-OVH backend, fall back and warn.

    ``list`` still works against class defaults plus env / ~/.ovh.conf
    credentials, but the user's ``--provider`` selection did not have the
    intended effect, so a warning is emitted to make the silent-fallback visible
    (distinct from the missing-block case, which is silent because it is the
    expected first-run shape).
    """
    ctx_with_provider = _temp_mngr_ctx_with_provider(temp_mngr_ctx, "ovh", LocalProviderConfig())

    resolved = _resolve_provider_config(ctx_with_provider, "ovh")

    assert resolved == OvhProviderConfig()
    assert len(log_warnings) == 1, f"expected exactly one warning, got {log_warnings!r}"
    assert "'ovh'" in log_warnings[0]
    assert "LocalProviderConfig" in log_warnings[0]
