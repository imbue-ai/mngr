from decimal import Decimal

import pytest

from imbue.mngr_imbue_cloud.errors import OvhCatalogPricingError
from imbue.mngr_imbue_cloud.pricing import compute_order_pricing

# OVH catalog prices are in micro-units: $1.00 == 100_000_000.
_USD = 100_000_000


def _renew(price: int, commitment: int, interval: int) -> dict:
    return {
        "capacities": ["renew"],
        "intervalUnit": "month",
        "interval": interval,
        "commitment": commitment,
        "price": price,
    }


def _install(price: int) -> dict:
    return {"capacities": ["installation"], "intervalUnit": "none", "interval": 0, "commitment": 0, "price": price}


def _rise2_catalog() -> dict:
    # Mirrors the real RISE-2 eco-catalog shape: $80/mo base (+$80 month-to-month
    # setup, waived on a 12-month commit), a +$13/mo 64GB RAM upgrade, $0 storage.
    return {
        "plans": [
            {
                "planCode": "24rise02-v1-us",
                "invoiceName": "RISE-2 | Intel Xeon-E 2388G",
                "pricings": [
                    _install(80 * _USD),
                    _renew(80 * _USD, 0, 1),
                    _install(0),
                    _renew(798 * _USD, 12, 12),
                ],
            }
        ],
        "addons": [
            {
                "planCode": "ram-64g-ecc-3200-24rise02-v1-us",
                "invoiceName": "64GB DDR4 ECC 3200MHz",
                "pricings": [_install(0), _renew(13 * _USD, 0, 1)],
            },
            {
                "planCode": "softraid-2x512nvme-24rise02-v1-us",
                "invoiceName": "2x SSD NVMe 512GB SoftRAID",
                "pricings": [_install(0), _renew(0, 0, 1)],
            },
        ],
    }


def test_compute_order_pricing_includes_addon_deltas_not_just_base() -> None:
    pricing = compute_order_pricing(
        _rise2_catalog(),
        "24rise02-v1-us",
        ["ram-64g-ecc-3200-24rise02-v1-us", "softraid-2x512nvme-24rise02-v1-us"],
    )
    # The exact bug this guards against: quoting the $80 base and dropping the $13 RAM upgrade.
    assert pricing.recurring_monthly == Decimal(93)
    assert pricing.recurring_monthly != Decimal(80)
    assert pricing.one_time_setup == Decimal(80)
    assert pricing.first_payment == Decimal(173)


def test_compute_order_pricing_lists_each_component_individually() -> None:
    pricing = compute_order_pricing(_rise2_catalog(), "24rise02-v1-us", ["ram-64g-ecc-3200-24rise02-v1-us"])
    line_item_by_code = {item.plan_code: item for item in pricing.line_items}
    assert line_item_by_code["24rise02-v1-us"].monthly == Decimal(80)
    assert line_item_by_code["24rise02-v1-us"].one_time_setup == Decimal(80)
    assert line_item_by_code["ram-64g-ecc-3200-24rise02-v1-us"].monthly == Decimal(13)
    assert line_item_by_code["ram-64g-ecc-3200-24rise02-v1-us"].one_time_setup == Decimal(0)


def test_compute_order_pricing_with_no_addons_is_base_only() -> None:
    pricing = compute_order_pricing(_rise2_catalog(), "24rise02-v1-us", [])
    assert pricing.recurring_monthly == Decimal(80)
    assert pricing.first_payment == Decimal(160)
    assert len(pricing.line_items) == 1


def test_compute_order_pricing_raises_for_unknown_plan() -> None:
    with pytest.raises(OvhCatalogPricingError):
        compute_order_pricing(_rise2_catalog(), "nonexistent-plan", [])


def test_compute_order_pricing_raises_for_unknown_addon() -> None:
    with pytest.raises(OvhCatalogPricingError):
        compute_order_pricing(_rise2_catalog(), "24rise02-v1-us", ["ram-512g-not-real"])


def test_compute_order_pricing_raises_when_no_month_to_month_price() -> None:
    catalog = {
        "plans": [
            {
                "planCode": "committed-only",
                "invoiceName": "Committed Only",
                "pricings": [_renew(798 * _USD, 12, 12)],
            }
        ],
        "addons": [],
    }
    with pytest.raises(OvhCatalogPricingError):
        compute_order_pricing(catalog, "committed-only", [])


def test_setup_fee_uses_month_to_month_charge_not_committed_waiver() -> None:
    # Plan declares an $80 month-to-month setup and $0 committed setup; we charge $80.
    pricing = compute_order_pricing(_rise2_catalog(), "24rise02-v1-us", [])
    assert pricing.one_time_setup == Decimal(80)
