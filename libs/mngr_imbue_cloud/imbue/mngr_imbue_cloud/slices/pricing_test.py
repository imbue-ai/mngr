from decimal import Decimal

import pytest

from imbue.mngr_imbue_cloud.errors import OvhCatalogPricingError
from imbue.mngr_imbue_cloud.slices.pricing import compute_order_pricing
from imbue.mngr_imbue_cloud.slices.pricing import compute_slice_pricing_rows
from imbue.mngr_imbue_cloud.slices.pricing import compute_storage_usable_gb
from imbue.mngr_imbue_cloud.slices.pricing import describe_storage_raid_level
from imbue.mngr_imbue_cloud.slices.pricing import parse_availability_delivery
from imbue.mngr_imbue_cloud.slices.pricing import parse_memory_gb
from imbue.mngr_imbue_cloud.slices.pricing import parse_storage_disk_groups

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


@pytest.mark.parametrize(
    "memory_code, expected_gb",
    [
        ("ram-64g-ecc-3200-24rise02-v1-us", 64),
        ("ram-32g-ecc-3200-24rise02-v1-us", 32),
        ("ram-128g-ecc-2933-24rise02-v1-us", 128),
    ],
)
def test_parse_memory_gb_extracts_ram_size(memory_code: str, expected_gb: int) -> None:
    assert parse_memory_gb(memory_code) == expected_gb


def test_parse_memory_gb_raises_on_unparseable_code() -> None:
    with pytest.raises(OvhCatalogPricingError):
        parse_memory_gb("storage-only-no-ram")


def test_parse_storage_disk_groups_handles_single_and_hybrid() -> None:
    assert parse_storage_disk_groups("softraid-2x512nvme") == ((2, 512),)
    assert parse_storage_disk_groups("hybridsoftraid-2x6000sa-2x512nvme") == ((2, 6000), (2, 512))


# Usable capacity is mirror-based: even groups halve (RAID1/RAID10), odd groups lose one disk to
# parity (RAID5-style), and a hybrid sums the usable of each group (2x6000 -> 6000, 2x512 -> 512).
@pytest.mark.parametrize(
    "storage_code, expected_usable_gb",
    [
        ("softraid-2x512nvme", 512),
        ("softraid-4x3840nvme", 7680),
        ("softraid-3x1920nvme", 3840),
        ("hybridsoftraid-2x6000sa-2x512nvme", 6512),
    ],
)
def test_compute_storage_usable_gb(storage_code: str, expected_usable_gb: int) -> None:
    assert compute_storage_usable_gb(storage_code) == expected_usable_gb


@pytest.mark.parametrize(
    "storage_code, expected_raid",
    [
        ("softraid-2x512nvme", "RAID1"),
        ("softraid-4x960nvme", "RAID10"),
        ("softraid-3x1920nvme", "RAID5"),
        ("hybridsoftraid-2x6000sa-2x512nvme", "MIXED"),
    ],
)
def test_describe_storage_raid_level(storage_code: str, expected_raid: str) -> None:
    assert describe_storage_raid_level(storage_code) == expected_raid


@pytest.mark.parametrize(
    "status, expected",
    [
        ("1H-low", (1, "low")),
        ("1H-high", (1, "high")),
        ("72H", (72, "")),
        ("1440H", (1440, "")),
        ("unavailable", (0, "")),
    ],
)
def test_parse_availability_delivery(status: str, expected: tuple[int, str]) -> None:
    assert parse_availability_delivery(status) == expected


def _slice_catalog() -> dict:
    # RISE-2 with a memory family (32/64GB) and a storage family (2x512nvme/2x1920nvme),
    # plus a product blob carrying CPU specs, as the pricing-rows builder needs.
    return {
        "products": [
            {
                "name": "24rise02",
                "description": "Intel Xeon-E 2388G",
                "blobs": {"technical": {"server": {"cpu": {"cores": 8, "threads": 16}}}},
            }
        ],
        "plans": [
            {
                "planCode": "24rise02-v1-us",
                "invoiceName": "RISE-2 | Intel Xeon-E 2388G",
                "product": "24rise02",
                "pricings": [_install(80 * _USD), _renew(80 * _USD, 0, 1)],
                "addonFamilies": [
                    {
                        "name": "memory",
                        "addons": ["ram-32g-ecc-3200-24rise02-v1-us", "ram-64g-ecc-3200-24rise02-v1-us"],
                    },
                    {
                        "name": "storage",
                        "addons": ["softraid-2x512nvme-24rise02-v1-us", "softraid-2x1920nvme-24rise02-v1-us"],
                    },
                ],
            }
        ],
        "addons": [
            {
                "planCode": "ram-32g-ecc-3200-24rise02-v1-us",
                "invoiceName": "32GB DDR4 ECC 3200MHz",
                "pricings": [_install(0), _renew(0, 0, 1)],
            },
            {
                "planCode": "ram-64g-ecc-3200-24rise02-v1-us",
                "invoiceName": "64GB DDR4 ECC 3200MHz",
                "pricings": [_install(0), _renew(13 * _USD, 0, 1)],
            },
            {
                "planCode": "softraid-2x512nvme-24rise02-v1-us",
                "invoiceName": "2x512 NVMe SoftRAID",
                "pricings": [_install(0), _renew(0, 0, 1)],
            },
            {
                "planCode": "softraid-2x1920nvme-24rise02-v1-us",
                "invoiceName": "2x1920 NVMe SoftRAID",
                "pricings": [_install(0), _renew(36 * _USD, 0, 1)],
            },
        ],
    }


def _slice_availabilities() -> list[dict]:
    return [
        {
            "planCode": "24rise02-v1-us",
            "memory": "ram-32g-ecc-3200",
            "storage": "softraid-2x512nvme",
            "datacenters": [
                {"datacenter": "vin", "availability": "72H"},
                {"datacenter": "hil", "availability": "1H-low"},
            ],
        },
        {
            "planCode": "24rise02-v1-us",
            "memory": "ram-32g-ecc-3200",
            "storage": "softraid-2x1920nvme",
            "datacenters": [{"datacenter": "vin", "availability": "1H-high"}],
        },
        {
            "planCode": "24rise02-v1-us",
            "memory": "ram-64g-ecc-3200",
            "storage": "softraid-2x512nvme",
            "datacenters": [{"datacenter": "vin", "availability": "1H-low"}],
        },
    ]


def test_compute_slice_pricing_rows_sorts_by_price_per_slice_and_computes_sizing() -> None:
    rows = compute_slice_pricing_rows(
        _slice_catalog(), _slice_availabilities(), {"vin", "hil"}, memory_per_slice_gb=8, cpu_overcommit_ratio=2.0
    )
    # 64GB is vin-only; 32GB is in both regions -> 3 rows (one per server x RAM x region), cheapest first.
    assert {(row.server_ram_gb, row.region) for row in rows} == {(64, "vin"), (32, "vin"), (32, "hil")}

    cheapest = rows[0]
    assert cheapest.plan_code == "24rise02-v1-us"
    assert cheapest.server_ram_gb == 64
    assert cheapest.region == "vin"
    # 64GB box, 8GB slices: (64-8)*1024 // (8*1024 + 512) = 6 slots after host reserve.
    assert cheapest.slot_count == 6
    # floor(16 threads * 2.0 overcommit / 6 slots) = 5 vCPUs; (512 - max(20, ceil(512*0.10))=52) // 6 = 76 GiB.
    assert cheapest.cpus_per_slice == 5
    assert cheapest.disk_gb_per_slice == 76
    # $93/mo (80 base + 13 RAM) + $80 setup amortized over 12 months, divided by 6 slots.
    assert cheapest.recurring_monthly_usd == Decimal(93)
    assert cheapest.price_per_slice_usd == Decimal("16.61")
    assert cheapest.delivery_hours == 1 and cheapest.stock_level == "low"
    # Only the base storage is available for the 64GB config, so there are no upgrade options.
    assert cheapest.storage_options == ()


def test_compute_slice_pricing_rows_splits_rows_per_region_with_distinct_delivery() -> None:
    rows = compute_slice_pricing_rows(
        _slice_catalog(), _slice_availabilities(), {"vin", "hil"}, memory_per_slice_gb=8, cpu_overcommit_ratio=2.0
    )
    by_region = {row.region: row for row in rows if row.server_ram_gb == 32}
    # 32GB/2x512nvme is 72H in vin but 1H-low in hil, so the per-region rows carry different delivery times.
    assert by_region["vin"].delivery_hours == 72 and by_region["vin"].stock_level == ""
    assert by_region["hil"].delivery_hours == 1 and by_region["hil"].stock_level == "low"


def test_compute_slice_pricing_rows_lists_storage_upgrades_as_per_slice_deltas() -> None:
    rows = compute_slice_pricing_rows(
        _slice_catalog(), _slice_availabilities(), {"vin", "hil"}, memory_per_slice_gb=8, cpu_overcommit_ratio=2.0
    )
    # The 2x1920nvme upgrade is only available in vin for the 32GB config.
    thirty_two_gb_vin = next(row for row in rows if row.server_ram_gb == 32 and row.region == "vin")
    assert thirty_two_gb_vin.base_storage_label == "softraid-2x512nvme"
    assert len(thirty_two_gb_vin.storage_options) == 1
    upgrade = thirty_two_gb_vin.storage_options[0]
    assert upgrade.label == "softraid-2x1920nvme"
    assert upgrade.storage_plan_code == "softraid-2x1920nvme-24rise02-v1-us"
    assert upgrade.usable_disk_gb == 1920
    # 1408 extra usable GB over the 512GB base, spread across 4 slots = 352 GB/slice; $36/mo / 1408 GB.
    # 32GB box -> 2 slots, so the extra 1408 GiB of usable disk splits 2 ways.
    assert upgrade.extra_disk_gb_per_slice == (1920 - 512) // 2
    assert upgrade.extra_monthly_usd == Decimal(36)
    assert upgrade.dollars_per_extra_gb == Decimal("0.0256")


def test_compute_slice_pricing_rows_filters_by_region() -> None:
    rows = compute_slice_pricing_rows(
        _slice_catalog(), _slice_availabilities(), {"hil"}, memory_per_slice_gb=8, cpu_overcommit_ratio=2.0
    )
    # Only the 32GB/2x512nvme combo is available in hil; the 64GB combo (vin-only) is excluded.
    assert [(row.server_ram_gb, row.region) for row in rows] == [(32, "hil")]


def test_compute_slice_pricing_rows_uses_cheapest_viable_storage_when_smallest_is_too_small() -> None:
    # 128GB -> 16 slots. The cheapest storage (2x512 = 512GB) gives (512-20)/16 = 30 GiB/slice, below the
    # 32 GiB boot disk, so it can't host a slice -- but 2x1920 can. The row must survive on the bigger storage.
    catalog = {
        "products": [
            {
                "name": "p",
                "description": "CPU",
                "blobs": {"technical": {"server": {"cpu": {"cores": 8, "threads": 16}}}},
            }
        ],
        "plans": [
            {
                "planCode": "plan-128-us",
                "invoiceName": "P",
                "product": "p",
                "pricings": [_install(0), _renew(100 * _USD, 0, 1)],
                "addonFamilies": [
                    {"name": "memory", "addons": ["ram-128g-ecc-3200-plan-128-us"]},
                    {
                        "name": "storage",
                        "addons": ["softraid-2x512nvme-plan-128-us", "softraid-2x1920nvme-plan-128-us"],
                    },
                ],
            }
        ],
        "addons": [
            {
                "planCode": "ram-128g-ecc-3200-plan-128-us",
                "invoiceName": "128GB",
                "pricings": [_install(0), _renew(0, 0, 1)],
            },
            {
                "planCode": "softraid-2x512nvme-plan-128-us",
                "invoiceName": "2x512",
                "pricings": [_install(0), _renew(0, 0, 1)],
            },
            {
                "planCode": "softraid-2x1920nvme-plan-128-us",
                "invoiceName": "2x1920",
                "pricings": [_install(0), _renew(40 * _USD, 0, 1)],
            },
        ],
    }
    availabilities = [
        {
            "planCode": "plan-128-us",
            "memory": "ram-128g-ecc-3200",
            "storage": storage,
            "datacenters": [{"datacenter": "vin", "availability": "1H-high"}],
        }
        for storage in ("softraid-2x512nvme", "softraid-2x1920nvme")
    ]
    rows = compute_slice_pricing_rows(
        catalog, availabilities, {"vin"}, memory_per_slice_gb=8, cpu_overcommit_ratio=2.0
    )
    assert len(rows) == 1
    row = rows[0]
    # 128GB box, 8GB slices: (128-8)*1024 // (8*1024 + 512) = 14 slots after host reserve.
    assert row.slot_count == 14
    # The too-small 2x512 is skipped; the base is the cheapest storage that can actually host a slice.
    assert row.base_storage_label == "softraid-2x1920nvme"
    # reserve = max(20, ceil(1920*0.10)) = 192; (1920 - 192) // 14 budget per slice.
    assert row.disk_gb_per_slice == (1920 - 192) // 14
    assert row.recurring_monthly_usd == Decimal(140)
    # The smaller (unsliceable) storage is not offered as an upgrade.
    assert row.storage_options == ()


def test_compute_slice_pricing_rows_matches_addons_whose_suffix_differs_from_plan_code() -> None:
    # SYS RAM/storage add-ons carry a '-24sys-us' family suffix, not the '-24sys012-v1-us' planCode;
    # the builder must still match them (regression guard for the bug where only RISE plans appeared).
    catalog = {
        "products": [
            {
                "name": "24sys",
                "description": "Intel Xeon-E 2136",
                "blobs": {"technical": {"server": {"cpu": {"cores": 6, "threads": 12}}}},
            }
        ],
        "plans": [
            {
                "planCode": "24sys012-v1-us",
                "invoiceName": "SYS-1",
                "product": "24sys",
                "pricings": [_install(0), _renew(30 * _USD, 0, 1)],
                "addonFamilies": [
                    {"name": "memory", "addons": ["ram-32g-ecc-2666-24sys-us"]},
                    {"name": "storage", "addons": ["softraid-2x512nvme-24sys-us"]},
                ],
            }
        ],
        "addons": [
            {
                "planCode": "ram-32g-ecc-2666-24sys-us",
                "invoiceName": "32GB",
                "pricings": [_install(0), _renew(0, 0, 1)],
            },
            {
                "planCode": "softraid-2x512nvme-24sys-us",
                "invoiceName": "2x512",
                "pricings": [_install(0), _renew(0, 0, 1)],
            },
        ],
    }
    availabilities = [
        {
            "planCode": "24sys012-v1-us",
            "memory": "ram-32g-ecc-2666",
            "storage": "softraid-2x512nvme",
            "datacenters": [{"datacenter": "vin", "availability": "1H-high"}],
        }
    ]
    rows = compute_slice_pricing_rows(
        catalog, availabilities, {"vin"}, memory_per_slice_gb=8, cpu_overcommit_ratio=2.0
    )
    assert len(rows) == 1
    assert rows[0].plan_code == "24sys012-v1-us"
    assert rows[0].server_ram_gb == 32
    assert rows[0].recurring_monthly_usd == Decimal(30)


def test_compute_slice_pricing_rows_skips_when_slice_larger_than_server_ram() -> None:
    rows = compute_slice_pricing_rows(
        _slice_catalog(), _slice_availabilities(), {"vin", "hil"}, memory_per_slice_gb=128, cpu_overcommit_ratio=2.0
    )
    assert rows == []
