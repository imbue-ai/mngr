import time
from typing import cast

import pytest

from imbue.mngr_imbue_cloud.errors import BareMetalConfigError
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.slices.ordering import _ReinstallStartAttempt
from imbue.mngr_imbue_cloud.slices.ordering import _looks_like_service_name
from imbue.mngr_imbue_cloud.slices.ordering import _read_address_tolerating_missing_service
from imbue.mngr_imbue_cloud.slices.ordering import build_box_host_key_postinstall_script
from imbue.mngr_imbue_cloud.slices.ordering import derive_server_specs
from imbue.mngr_imbue_cloud.slices.ordering import extract_order_id
from imbue.mngr_imbue_cloud.slices.ordering import select_eco_option_codes
from imbue.mngr_imbue_cloud.slices.ordering import start_os_reinstall
from imbue.mngr_imbue_cloud.slices.ordering import summarize_checkout_prices
from imbue.mngr_imbue_cloud.slices.ordering import wait_for_dedicated_server_address
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_vps.errors import VpsApiError


def _eco_options() -> list[dict]:
    return [
        {"family": "bandwidth", "planCode": "bandwidth-1000-unguaranteed-rise-gen2-us", "mandatory": True},
        {"family": "vrack", "planCode": "vrack-bandwidth-1000-24rise01-v1-us", "mandatory": True},
        {"family": "memory", "planCode": "ram-32g-ecc-3200-24rise01-v1-us", "mandatory": True},
        {"family": "memory", "planCode": "ram-64g-ecc-3200-24rise01-v1-us", "mandatory": True},
        {"family": "memory", "planCode": "ram-128g-ecc-2933-24rise01-v1-us", "mandatory": True},
        {"family": "storage", "planCode": "softraid-2x512nvme-24rise01-v1-us", "mandatory": True},
        {"family": "storage", "planCode": "softraid-2x1920nvme-24rise01-v1-us", "mandatory": True},
    ]


def test_select_eco_option_codes_picks_requested_memory_storage_plus_single_offer_families() -> None:
    codes = select_eco_option_codes(
        _eco_options(), memory_gb=64, storage_short="softraid-2x512nvme", explicit_option_codes=()
    )
    assert set(codes) == {
        "ram-64g-ecc-3200-24rise01-v1-us",
        "softraid-2x512nvme-24rise01-v1-us",
        "bandwidth-1000-unguaranteed-rise-gen2-us",
        "vrack-bandwidth-1000-24rise01-v1-us",
    }


def test_select_eco_option_codes_raises_for_unavailable_memory() -> None:
    with pytest.raises(BareMetalConfigError):
        select_eco_option_codes(
            _eco_options(), memory_gb=256, storage_short="softraid-2x512nvme", explicit_option_codes=()
        )


def test_select_eco_option_codes_raises_for_unavailable_storage() -> None:
    with pytest.raises(BareMetalConfigError):
        select_eco_option_codes(
            _eco_options(), memory_gb=64, storage_short="softraid-4x3840nvme", explicit_option_codes=()
        )


def test_select_eco_option_codes_raises_when_multi_offer_family_has_no_explicit_choice() -> None:
    # Two bandwidth offers and no --option: refuse rather than pick one on the operator's behalf.
    options = _eco_options() + [
        {"family": "bandwidth", "planCode": "bandwidth-3000-unguaranteed-rise-gen2-us", "mandatory": True}
    ]
    with pytest.raises(BareMetalConfigError):
        select_eco_option_codes(options, memory_gb=64, storage_short="softraid-2x512nvme", explicit_option_codes=())


def _priced_option(family: str, plan_code: str, monthly_usd: str) -> dict:
    # Shape mirrors the OVH `GET /order/cart/{id}/eco/options` payload: each offer carries a `prices`
    # list keyed by pricingMode + duration. We only price the month-to-month (default / P1M) entry.
    return {
        "family": family,
        "planCode": plan_code,
        "mandatory": True,
        "prices": [{"pricingMode": "default", "duration": "P1M", "price": {"value": float(monthly_usd)}}],
    }


def _multi_offer_options() -> list[dict]:
    # Models the 24sys032-us plan: bandwidth + vrack are each mandatory with a free baseline + a paid upgrade.
    return [
        _priced_option("memory", "ram-128g-ecc-2666-24sys-us", "40.00"),
        _priced_option("storage", "softraid-2x960nvme-24sys-us", "0.00"),
        _priced_option("bandwidth", "bandwidth-1000-24sys-us", "0.00"),
        _priced_option("bandwidth", "bandwidth-2000-24sys-us", "120.00"),
        _priced_option("vrack", "vrack-bandwidth-500-24sys-us", "0.00"),
        _priced_option("vrack", "vrack-bandwidth-1000-24sys-us", "23.00"),
    ]


def test_select_eco_option_codes_uses_explicit_choices_for_multi_offer_families() -> None:
    codes = select_eco_option_codes(
        _multi_offer_options(),
        memory_gb=128,
        storage_short="softraid-2x960nvme",
        explicit_option_codes=("bandwidth-1000-24sys-us", "vrack-bandwidth-500-24sys-us"),
    )
    assert set(codes) == {
        "ram-128g-ecc-2666-24sys-us",
        "softraid-2x960nvme-24sys-us",
        "bandwidth-1000-24sys-us",
        "vrack-bandwidth-500-24sys-us",
    }


def test_select_eco_option_codes_can_pick_the_paid_upgrade_when_named() -> None:
    # Explicit selection is honored verbatim -- the operator can choose the paid tier, not just the free one.
    codes = select_eco_option_codes(
        _multi_offer_options(),
        memory_gb=128,
        storage_short="softraid-2x960nvme",
        explicit_option_codes=("bandwidth-2000-24sys-us", "vrack-bandwidth-500-24sys-us"),
    )
    assert "bandwidth-2000-24sys-us" in codes
    assert "bandwidth-1000-24sys-us" not in codes


def test_select_eco_option_codes_raises_when_multi_offer_family_left_unspecified() -> None:
    # vrack still ambiguous (no --option for it): refuse even though bandwidth was specified.
    with pytest.raises(BareMetalConfigError):
        select_eco_option_codes(
            _multi_offer_options(),
            memory_gb=128,
            storage_short="softraid-2x960nvme",
            explicit_option_codes=("bandwidth-1000-24sys-us",),
        )


def test_select_eco_option_codes_raises_when_two_offers_named_for_one_family() -> None:
    with pytest.raises(BareMetalConfigError):
        select_eco_option_codes(
            _multi_offer_options(),
            memory_gb=128,
            storage_short="softraid-2x960nvme",
            explicit_option_codes=(
                "bandwidth-1000-24sys-us",
                "bandwidth-2000-24sys-us",
                "vrack-bandwidth-500-24sys-us",
            ),
        )


def test_select_eco_option_codes_raises_for_unknown_explicit_option() -> None:
    with pytest.raises(BareMetalConfigError):
        select_eco_option_codes(
            _multi_offer_options(),
            memory_gb=128,
            storage_short="softraid-2x960nvme",
            explicit_option_codes=(
                "bandwidth-1000-24sys-us",
                "vrack-bandwidth-500-24sys-us",
                "bogus-addon-24sys-us",
            ),
        )


def test_select_eco_option_codes_handles_plan_without_vrack() -> None:
    # The cheaper SK line (e.g. 24sk602-v1-us) ships no vrack family at all; ordering must still succeed.
    options = [
        {"family": "bandwidth", "planCode": "bandwidth-500-25sk-us", "mandatory": True},
        {"family": "memory", "planCode": "ram-128g-ecc-2400-24sk60-us", "mandatory": True},
        {"family": "memory", "planCode": "ram-256g-ecc-2400-24sk60-us", "mandatory": True},
        {"family": "storage", "planCode": "softraid-2x8000sa-24sk60-us", "mandatory": True},
    ]
    codes = select_eco_option_codes(
        options, memory_gb=256, storage_short="softraid-2x8000sa", explicit_option_codes=()
    )
    assert set(codes) == {
        "ram-256g-ecc-2400-24sk60-us",
        "softraid-2x8000sa-24sk60-us",
        "bandwidth-500-25sk-us",
    }


def test_select_eco_option_codes_skips_optional_addon_families() -> None:
    # An optional (mandatory=False) single-offer add-on family must never be auto-picked into the cart.
    options = _eco_options() + [{"family": "backup", "planCode": "backup-storage-500-us", "mandatory": False}]
    codes = select_eco_option_codes(
        options, memory_gb=64, storage_short="softraid-2x512nvme", explicit_option_codes=()
    )
    assert "backup-storage-500-us" not in codes


def test_extract_order_id_parses_int() -> None:
    assert extract_order_id({"orderId": "8144904"}) == 8144904


def test_extract_order_id_raises_when_missing() -> None:
    with pytest.raises(BareMetalProvisioningError):
        extract_order_id({"url": "https://..."})


@pytest.mark.parametrize(
    "candidate, expected",
    [
        ("ns1012536.ip-15-204-140.us", True),
        ("*", False),
        ("eco", False),
        ("", False),
        (None, False),
    ],
)
def test_looks_like_service_name(candidate: object, expected: bool) -> None:
    assert _looks_like_service_name(candidate) is expected


def test_summarize_checkout_prices_renders_due_now_from_price_dict() -> None:
    preview = {
        "prices": {
            "withoutTax": {"text": "$153.00 USD"},
            "tax": {"text": "$0.00 USD"},
            "withTax": {"text": "$153.00 USD"},
        }
    }
    summary = summarize_checkout_prices(preview)
    assert "due now: $153.00 USD" in summary


def test_derive_server_specs_reads_cpu_from_product_and_disk_from_storage() -> None:
    catalog = {
        "products": [{"name": "24rise01", "blobs": {"technical": {"server": {"cpu": {"cores": 6, "threads": 12}}}}}],
        "plans": [{"planCode": "24rise01-v1-us", "product": "24rise01"}],
    }
    cores, threads, disk_gb, raid = derive_server_specs(catalog, "24rise01-v1-us", "softraid-2x512nvme")
    assert (cores, threads, disk_gb, raid) == (6, 12, 512, "RAID1")


def test_derive_server_specs_raises_when_cpu_specs_absent() -> None:
    catalog = {"products": [{"name": "x", "blobs": {}}], "plans": [{"planCode": "p", "product": "x"}]}
    with pytest.raises(BareMetalConfigError):
        derive_server_specs(catalog, "p", "softraid-2x512nvme")


def test_build_box_host_key_postinstall_script_installs_ed25519_and_drops_other_types() -> None:
    script = build_box_host_key_postinstall_script(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEKEYBODY\n-----END OPENSSH PRIVATE KEY-----",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5FAKE box",
    )
    # Writes our generated host key into /etc/ssh ...
    assert "/etc/ssh/ssh_host_ed25519_key" in script
    assert "FAKEKEYBODY" in script
    assert "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5FAKE box" in script
    assert "chmod 600 /etc/ssh/ssh_host_ed25519_key" in script
    # ... removes the other host key types (ed25519-only) ...
    assert "ssh_host_rsa_key" in script
    assert "ssh_host_ecdsa_key" in script
    # ... and restarts sshd so the new key takes effect.
    assert "restart" in script and "ssh" in script


class _FakeReinstallClient:
    """Records the reinstall call body and returns a scripted task id."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call_api(self, method: str, path: str, **body: object) -> dict:
        self.calls.append({"method": method, "path": path, **body})
        return {"taskId": 4242}


def test_start_os_reinstall_injects_a_known_host_key_and_returns_its_public_half() -> None:
    client = _FakeReinstallClient()
    result = start_os_reinstall(
        cast(OvhVpsClient, client), service_name="ns1.example", ssh_public_key="ssh-ed25519 AAAAclient"
    )
    assert result.task_id == 4242
    # The reinstall request injects our login key AND a post-install script (the
    # private host-key delivery channel), and we get back the host PUBLIC key to pin.
    customizations = client.calls[0]["customizations"]
    assert customizations["sshKey"] == "ssh-ed25519 AAAAclient"
    assert customizations["postInstallationScript"]
    assert result.box_host_public_key.startswith("ssh-ed25519 ")


class _RaisingReinstallClient:
    """Fake OVH client whose reinstall POST always raises a scripted ``VpsApiError``."""

    def __init__(self, error: VpsApiError) -> None:
        self._error = error
        self.call_count = 0

    def call_api(self, method: str, path: str, **body: object) -> dict:
        self.call_count += 1
        raise self._error


def _reinstall_attempt(client: object) -> _ReinstallStartAttempt:
    return _ReinstallStartAttempt(
        client=client,
        service_name="ns1.example",
        os_template="debian12_64",
        customizations={"sshKey": "ssh-ed25519 AAAAclient", "postInstallationScript": "x"},
    )


def test_reinstall_start_attempt_wraps_task_on_success() -> None:
    # A successful POST is wrapped in a one-tuple so poll_for_value stops retrying.
    assert _reinstall_attempt(_FakeReinstallClient())() == ({"taskId": 4242},)


def test_reinstall_start_attempt_retries_on_transient_compatibility_error() -> None:
    # OVH's transient OS-compatibility-lookup failure signals a retry (None), not an error.
    transient = VpsApiError(
        0,
        "OVH API POST /dedicated/server/ns1.example/reinstall returned error: "
        "Error while retrieving compatibility details for operating system debian12_64",
    )
    client = _RaisingReinstallClient(transient)
    assert _reinstall_attempt(client)() is None
    assert client.call_count == 1


def test_reinstall_start_attempt_propagates_non_transient_error() -> None:
    # Any other OVH error is not swallowed -- it propagates so setup fails loudly.
    fatal = VpsApiError(404, "OVH API POST /dedicated/server/ns1.example/reinstall returned error: not found")
    with pytest.raises(VpsApiError):
        _reinstall_attempt(_RaisingReinstallClient(fatal))()


class _ScriptedDedicatedServerClient:
    """Fake OVH client returning a scripted sequence of dedicated-server GET outcomes.

    Each entry is either a ``VpsApiError`` to raise or a body dict to return, so a test
    can reproduce the real delivery sequence: 404 while OVH has not published the
    service, then a body with no ``ip``, then the assigned address. A call past the end
    of the script fails the test rather than inventing a response, so a regression that
    polls more times than the scenario describes says so instead of spinning until the
    poll loop times out.
    """

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.call_count = 0

    def call_api(self, method: str, path: str, **body: object) -> dict:
        self.call_count += 1
        assert self._outcomes, f"unscripted call {self.call_count}: {method} {path}"
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, VpsApiError):
            raise outcome
        return cast(dict, outcome)


def _not_found() -> VpsApiError:
    return VpsApiError(404, "OVH API GET /dedicated/server/ns1.example returned error: This service does not exist")


def test_read_address_tolerating_missing_service_treats_a_404_as_not_ready_yet() -> None:
    # The real race: OVH assigns the serviceName on the order before the service is
    # queryable, so the first reads 404. Inside the grace window that is "not yet".
    client = _ScriptedDedicatedServerClient([_not_found()])
    result = _read_address_tolerating_missing_service(
        cast(OvhVpsClient, client), "ns1.example", grace_started_at=time.monotonic(), visibility_grace_seconds=600.0
    )
    assert result is None


def test_read_address_tolerating_missing_service_raises_once_the_grace_window_has_passed() -> None:
    # A serviceName that never materializes must surface promptly rather than polling
    # silently until the 4h delivery timeout.
    client = _ScriptedDedicatedServerClient([_not_found()])
    with pytest.raises(BareMetalProvisioningError) as exc_info:
        _read_address_tolerating_missing_service(
            cast(OvhVpsClient, client),
            "ns1.example",
            grace_started_at=time.monotonic() - 601.0,
            visibility_grace_seconds=600.0,
        )
    # The message must name the window that was actually applied, not a module default.
    assert "ns1.example was still not queryable 600s" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, VpsApiError)


def test_read_address_tolerating_missing_service_propagates_non_404_errors() -> None:
    # Only absence is a race; auth/quota/server errors are real and must not be swallowed.
    client = _ScriptedDedicatedServerClient([VpsApiError(403, "forbidden")])
    with pytest.raises(VpsApiError) as exc_info:
        _read_address_tolerating_missing_service(
            cast(OvhVpsClient, client),
            "ns1.example",
            grace_started_at=time.monotonic(),
            visibility_grace_seconds=600.0,
        )
    assert exc_info.value.status_code == 403


def test_read_address_tolerating_missing_service_returns_none_when_published_without_an_ip() -> None:
    client = _ScriptedDedicatedServerClient([{"state": "ok"}])
    result = _read_address_tolerating_missing_service(
        cast(OvhVpsClient, client), "ns1.example", grace_started_at=time.monotonic(), visibility_grace_seconds=600.0
    )
    assert result is None


def test_read_address_tolerating_missing_service_returns_the_address_once_assigned() -> None:
    client = _ScriptedDedicatedServerClient([{"ip": "51.81.154.217"}])
    result = _read_address_tolerating_missing_service(
        cast(OvhVpsClient, client), "ns1.example", grace_started_at=time.monotonic(), visibility_grace_seconds=600.0
    )
    assert result == "51.81.154.217"


def test_wait_for_dedicated_server_address_polls_through_a_404_to_the_address() -> None:
    # End-to-end through the poll loop: 404, then no-ip, then the address -- the exact
    # sequence a real delivery produced, which previously aborted on the first 404.
    client = _ScriptedDedicatedServerClient([_not_found(), {"state": "ok"}, {"ip": "51.81.154.217"}])
    address = wait_for_dedicated_server_address(
        cast(OvhVpsClient, client),
        service_name="ns1.example",
        timeout_seconds=30.0,
        visibility_grace_seconds=600.0,
        poll_interval_seconds=0.01,
    )
    assert address == "51.81.154.217"
    assert client.call_count == 3
