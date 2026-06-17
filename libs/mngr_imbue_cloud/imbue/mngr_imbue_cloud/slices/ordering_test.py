import pytest

from imbue.mngr_imbue_cloud.errors import BareMetalConfigError
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.slices.ordering import _looks_like_service_name
from imbue.mngr_imbue_cloud.slices.ordering import derive_server_specs
from imbue.mngr_imbue_cloud.slices.ordering import extract_order_id
from imbue.mngr_imbue_cloud.slices.ordering import select_eco_option_codes
from imbue.mngr_imbue_cloud.slices.ordering import summarize_checkout_prices


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
    codes = select_eco_option_codes(_eco_options(), memory_gb=64, storage_short="softraid-2x512nvme")
    assert set(codes) == {
        "ram-64g-ecc-3200-24rise01-v1-us",
        "softraid-2x512nvme-24rise01-v1-us",
        "bandwidth-1000-unguaranteed-rise-gen2-us",
        "vrack-bandwidth-1000-24rise01-v1-us",
    }


def test_select_eco_option_codes_raises_for_unavailable_memory() -> None:
    with pytest.raises(BareMetalConfigError):
        select_eco_option_codes(_eco_options(), memory_gb=256, storage_short="softraid-2x512nvme")


def test_select_eco_option_codes_raises_for_unavailable_storage() -> None:
    with pytest.raises(BareMetalConfigError):
        select_eco_option_codes(_eco_options(), memory_gb=64, storage_short="softraid-4x3840nvme")


def test_select_eco_option_codes_raises_when_mandatory_family_is_ambiguous_without_prices() -> None:
    # Two bandwidth offers with no comparable prices cannot be auto-picked: there is no basis to
    # tell the included baseline from a paid upgrade, so we refuse rather than guess.
    options = _eco_options() + [
        {"family": "bandwidth", "planCode": "bandwidth-3000-unguaranteed-rise-gen2-us", "mandatory": True}
    ]
    with pytest.raises(BareMetalConfigError):
        select_eco_option_codes(options, memory_gb=64, storage_short="softraid-2x512nvme")


def _priced_option(family: str, plan_code: str, monthly_usd: str) -> dict:
    # Shape mirrors the OVH `GET /order/cart/{id}/eco/options` payload: each offer carries a `prices`
    # list keyed by pricingMode + duration. We only price the month-to-month (default / P1M) entry.
    return {
        "family": family,
        "planCode": plan_code,
        "mandatory": True,
        "prices": [{"pricingMode": "default", "duration": "P1M", "price": {"value": float(monthly_usd)}}],
    }


def test_select_eco_option_codes_picks_cheapest_offer_for_multi_offer_mandatory_family() -> None:
    # Models the 24sys032-us plan: bandwidth + vrack are each mandatory with a free included baseline
    # plus a paid upgrade. Auto-pick must take the free baseline of each (what the pricing table quotes).
    options = [
        _priced_option("memory", "ram-128g-ecc-2666-24sys-us", "40.00"),
        _priced_option("storage", "softraid-2x960nvme-24sys-us", "0.00"),
        _priced_option("bandwidth", "bandwidth-1000-24sys-us", "0.00"),
        _priced_option("bandwidth", "bandwidth-2000-24sys-us", "120.00"),
        _priced_option("vrack", "vrack-bandwidth-500-24sys-us", "0.00"),
        _priced_option("vrack", "vrack-bandwidth-1000-24sys-us", "23.00"),
    ]
    codes = select_eco_option_codes(options, memory_gb=128, storage_short="softraid-2x960nvme")
    assert set(codes) == {
        "ram-128g-ecc-2666-24sys-us",
        "softraid-2x960nvme-24sys-us",
        "bandwidth-1000-24sys-us",
        "vrack-bandwidth-500-24sys-us",
    }


def test_select_eco_option_codes_raises_when_cheapest_offer_is_tied() -> None:
    # Two equally-priced offers in a mandatory family: refuse rather than silently pick one.
    options = [
        _priced_option("memory", "ram-128g-ecc-2666-24sys-us", "40.00"),
        _priced_option("storage", "softraid-2x960nvme-24sys-us", "0.00"),
        _priced_option("bandwidth", "bandwidth-1000-24sys-us", "0.00"),
        _priced_option("bandwidth", "bandwidth-1000b-24sys-us", "0.00"),
    ]
    with pytest.raises(BareMetalConfigError):
        select_eco_option_codes(options, memory_gb=128, storage_short="softraid-2x960nvme")


def test_select_eco_option_codes_handles_plan_without_vrack() -> None:
    # The cheaper SK line (e.g. 24sk602-v1-us) ships no vrack family at all; ordering must still succeed.
    options = [
        {"family": "bandwidth", "planCode": "bandwidth-500-25sk-us", "mandatory": True},
        {"family": "memory", "planCode": "ram-128g-ecc-2400-24sk60-us", "mandatory": True},
        {"family": "memory", "planCode": "ram-256g-ecc-2400-24sk60-us", "mandatory": True},
        {"family": "storage", "planCode": "softraid-2x8000sa-24sk60-us", "mandatory": True},
    ]
    codes = select_eco_option_codes(options, memory_gb=256, storage_short="softraid-2x8000sa")
    assert set(codes) == {
        "ram-256g-ecc-2400-24sk60-us",
        "softraid-2x8000sa-24sk60-us",
        "bandwidth-500-25sk-us",
    }


def test_select_eco_option_codes_skips_optional_addon_families() -> None:
    # An optional (mandatory=False) single-offer add-on family must never be auto-picked into the cart.
    options = _eco_options() + [{"family": "backup", "planCode": "backup-storage-500-us", "mandatory": False}]
    codes = select_eco_option_codes(options, memory_gb=64, storage_short="softraid-2x512nvme")
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
