"""Tests for the ``mngr ovh`` CLI subcommands."""

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import ovh
import pytest
from click.testing import CliRunner

from imbue.mngr_ovh.cli import ovh as ovh_group
from imbue.mngr_ovh.client import OvhVpsClient


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


def test_list_errors_clearly_when_unconfigured(clean_env: None) -> None:
    raw_client = MagicMock(spec=ovh.Client)
    raw_client.call = MagicMock(side_effect=AssertionError("no API call should fire when unconfigured"))
    placeholder = OvhVpsClient(ovh_client=raw_client, subsidiary="US", task_poll_interval=0.0, is_unconfigured=True)
    with patch("imbue.mngr_ovh.cli.build_ovh_client", return_value=placeholder):
        runner = CliRunner()
        result = runner.invoke(ovh_group, ["list"])
    assert result.exit_code != 0
    assert "OVH credentials not configured" in result.output


def test_list_with_no_vpses_prints_empty_message(clean_env: None) -> None:
    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path == "/vps":
            return []
        raise AssertionError(f"unexpected {method} {path}")

    with _patch_build_ovh_client(fake):
        runner = CliRunner()
        result = runner.invoke(ovh_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "no OVH VPSes" in result.output


def test_list_default_hides_untagged_vpses(clean_env: None) -> None:
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
        runner = CliRunner()
        result = runner.invoke(ovh_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "no mngr-tagged OVH VPSes" in result.output
    assert "vps-untagged.vps.ovh.us" not in result.output


def test_list_renders_tagged_vps(clean_env: None) -> None:
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
        runner = CliRunner()
        result = runner.invoke(ovh_group, ["list"])
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


def test_adopt_pending_order_errors_clearly_when_unconfigured(clean_env: None) -> None:
    raw_client = MagicMock(spec=ovh.Client)
    raw_client.call = MagicMock(side_effect=AssertionError("no API call should fire when unconfigured"))
    placeholder = OvhVpsClient(ovh_client=raw_client, subsidiary="US", task_poll_interval=0.0, is_unconfigured=True)
    with patch("imbue.mngr_ovh.cli.build_ovh_client", return_value=placeholder):
        runner = CliRunner()
        result = runner.invoke(
            ovh_group,
            ["adopt-pending-order", "--order-id", "42", "--provider-name", "ovh", "--timeout-seconds", "0.05"],
        )
    assert result.exit_code != 0
    assert "OVH credentials not configured" in result.output


def test_adopt_pending_order_reports_clearly_on_no_delivery(clean_env: None) -> None:
    """If the order still hasn't delivered within the supplied timeout, exit non-zero with guidance."""

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path == "/me/order/4242/details":
            return [101]
        if method == "GET" and path == "/me/order/4242/details/101/extension":
            return {
                "order": {
                    "plan": {
                        "code": "vps-2025-model1",
                        "duration": "P1M",
                        "product": {"name": "virtualPrivateServer"},
                    },
                },
            }
        if method == "GET" and path == "/me/order/4242/details/101/operations":
            return [201]
        if method == "GET" and path == "/me/order/4242/details/101/operations/201":
            return {"id": 201, "status": "doing", "resource": {}}
        raise AssertionError(f"unexpected call: {method} {path}")

    with (
        _patch_build_ovh_client(fake),
        patch("imbue.mngr_ovh.ordering._OVH_DELIVERY_POLL_INTERVAL_SECONDS", 0.0),
    ):
        runner = CliRunner()
        result = runner.invoke(
            ovh_group,
            ["adopt-pending-order", "--order-id", "4242", "--provider-name", "ovh", "--timeout-seconds", "0.05"],
        )
    assert result.exit_code != 0
    assert "still has not delivered" in result.output
    assert "4242" in result.output
