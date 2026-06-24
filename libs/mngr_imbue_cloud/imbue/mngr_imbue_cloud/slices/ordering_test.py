from typing import cast

import pytest

from imbue.mngr_imbue_cloud.errors import BareMetalConfigError
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.slices.ordering import _looks_like_service_name
from imbue.mngr_imbue_cloud.slices.ordering import build_box_host_key_postinstall_script
from imbue.mngr_imbue_cloud.slices.ordering import derive_server_specs
from imbue.mngr_imbue_cloud.slices.ordering import extract_order_id
from imbue.mngr_imbue_cloud.slices.ordering import select_eco_option_codes
from imbue.mngr_imbue_cloud.slices.ordering import start_os_reinstall
from imbue.mngr_imbue_cloud.slices.ordering import summarize_checkout_prices
from imbue.mngr_ovh.client import OvhVpsClient


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
