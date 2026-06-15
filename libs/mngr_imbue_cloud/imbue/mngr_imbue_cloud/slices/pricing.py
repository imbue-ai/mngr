import re
from collections.abc import Mapping
from collections.abc import Sequence
from decimal import Decimal
from typing import AbstractSet
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.data_types import OrderPricing
from imbue.mngr_imbue_cloud.data_types import PriceLineItem
from imbue.mngr_imbue_cloud.data_types import SlicePricingRow
from imbue.mngr_imbue_cloud.data_types import SliceStorageOption
from imbue.mngr_imbue_cloud.errors import BareMetalConfigError
from imbue.mngr_imbue_cloud.errors import OvhCatalogPricingError
from imbue.mngr_imbue_cloud.slices.bare_metal import choose_raid_level
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_disk_budget_gib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_disk_gib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_vcpus
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slot_count

# OVH catalog prices are integers scaled by 10^8 (e.g. $80.00 is stored as 8_000_000_000).
_OVH_PRICE_SCALE: Final[Decimal] = Decimal(10) ** 8


@pure
def _price_to_usd(scaled_price: Any) -> Decimal:
    # Decimal(str(...)) so an int or a float catalog value both convert exactly.
    return Decimal(str(scaled_price)) / _OVH_PRICE_SCALE


@pure
def _month_to_month_price_usd(entry: Mapping[str, Any]) -> Decimal | None:
    for pricing in entry.get("pricings", []):
        capacities = pricing.get("capacities", [])
        if (
            "renew" in capacities
            and pricing.get("intervalUnit") == "month"
            and pricing.get("interval") == 1
            and pricing.get("commitment", 0) == 0
        ):
            return _price_to_usd(pricing.get("price", 0))
    return None


@pure
def _setup_fee_usd(entry: Mapping[str, Any]) -> Decimal:
    # An eco/baremetal plan lists several installation entries: a non-zero fee for
    # the month-to-month term and 0 for the committed terms (which waive setup).
    # The month-to-month fee we actually charge is the largest, so take the max.
    fees = [
        _price_to_usd(pricing.get("price", 0))
        for pricing in entry.get("pricings", [])
        if "installation" in pricing.get("capacities", [])
    ]
    return max(fees) if fees else Decimal(0)


@pure
def _line_item_from_entry(entry: Mapping[str, Any]) -> PriceLineItem:
    monthly = _month_to_month_price_usd(entry)
    if monthly is None:
        raise OvhCatalogPricingError(
            f"OVH catalog entry {entry.get('planCode')!r} has no month-to-month (commitment=0) renew price"
        )
    return PriceLineItem(
        plan_code=str(entry["planCode"]),
        description=str(entry.get("invoiceName") or entry["planCode"]),
        monthly=monthly,
        one_time_setup=_setup_fee_usd(entry),
    )


@pure
def compute_order_pricing(
    catalog: Mapping[str, Any],
    plan_code: str,
    addon_codes: Sequence[str],
) -> OrderPricing:
    """Compute the true all-in month-to-month pricing for an OVH plan plus selected add-ons.

    ``recurring_monthly`` sums the base plan and every selected add-on delta, so the
    catalog's bare base price can never be mistaken for the real recurring cost (the
    mistake this helper exists to prevent). Raises ``OvhCatalogPricingError`` if the
    plan or any add-on is absent from the catalog or lacks a month-to-month renew price.
    """
    plan_by_code = {str(plan["planCode"]): plan for plan in catalog.get("plans", [])}
    addon_by_code = {str(addon["planCode"]): addon for addon in catalog.get("addons", [])}

    plan_entry = plan_by_code.get(plan_code)
    if plan_entry is None:
        raise OvhCatalogPricingError(f"plan {plan_code!r} not found in OVH catalog")

    # Price the base plan first, then every selected add-on as its own line item.
    line_items: list[PriceLineItem] = [_line_item_from_entry(plan_entry)]
    for addon_code in addon_codes:
        addon_entry = addon_by_code.get(addon_code)
        if addon_entry is None:
            raise OvhCatalogPricingError(
                f"add-on {addon_code!r} (selected for plan {plan_code!r}) not found in OVH catalog"
            )
        line_items.append(_line_item_from_entry(addon_entry))

    recurring_monthly = sum((item.monthly for item in line_items), Decimal(0))
    one_time_setup = sum((item.one_time_setup for item in line_items), Decimal(0))
    return OrderPricing(
        plan_code=plan_code,
        line_items=tuple(line_items),
        recurring_monthly=recurring_monthly,
        one_time_setup=one_time_setup,
        first_payment=recurring_monthly + one_time_setup,
    )


# Number of months over which a one-time setup fee is amortized into the per-slice monthly cost.
_SETUP_AMORTIZATION_MONTHS: Final[Decimal] = Decimal(12)

# OVH availability statuses that mean a (plan, memory, storage) combo is not orderable right now.
_UNORDERABLE_AVAILABILITY_STATUSES: Final[frozenset[str]] = frozenset({"unavailable", "comingSoon"})

_AVAILABILITY_DELIVERY_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)H", re.IGNORECASE)
_MEMORY_GB_RE: Final[re.Pattern[str]] = re.compile(r"ram-(\d+)g", re.IGNORECASE)
# Matches each disk group in a storage planCode, e.g. '2x512nvme' or the '2x6000sa' + '2x512nvme' of a hybrid.
_STORAGE_DISK_GROUP_RE: Final[re.Pattern[str]] = re.compile(r"(\d+)x(\d+)(nvme|ssd|sa)", re.IGNORECASE)


@pure
def parse_memory_gb(memory_addon_code: str) -> int:
    """Parse the RAM size in GB from a memory add-on planCode (e.g. 'ram-64g-ecc-3200-...' -> 64)."""
    match = _MEMORY_GB_RE.search(memory_addon_code)
    if match is None:
        raise OvhCatalogPricingError(f"could not parse RAM size from memory add-on {memory_addon_code!r}")
    return int(match.group(1))


@pure
def parse_storage_disk_groups(storage_code: str) -> tuple[tuple[int, int], ...]:
    """Parse a storage planCode into (disk_count, disk_gb) groups (e.g. '2x512nvme' -> ((2, 512),))."""
    groups = tuple((int(count), int(size)) for count, size, _media in _STORAGE_DISK_GROUP_RE.findall(storage_code))
    if not groups:
        raise OvhCatalogPricingError(f"could not parse storage layout from {storage_code!r}")
    return groups


@pure
def compute_storage_usable_gb(storage_code: str) -> int:
    """Usable GB after mirror-based RAID across all disk groups.

    Even counts mirror (RAID1 / RAID10) so usable is half; odd counts assume RAID5-style
    single-parity ((n-1) x size); a single disk has no redundancy and counts raw.
    """
    total_gb = 0
    for disk_count, disk_gb in parse_storage_disk_groups(storage_code):
        if disk_count < 2:
            total_gb += disk_count * disk_gb
        elif disk_count % 2 == 0:
            total_gb += (disk_count // 2) * disk_gb
        else:
            total_gb += (disk_count - 1) * disk_gb
    return total_gb


@pure
def describe_storage_raid_level(storage_code: str) -> str:
    """Best-effort RAID label for a storage config: RAID1/RAID10 for a single even group, MIXED for hybrids."""
    groups = parse_storage_disk_groups(storage_code)
    if len(groups) > 1:
        return "MIXED"
    total_disks = sum(count for count, _size in groups)
    try:
        return choose_raid_level(total_disks)
    except BareMetalConfigError:
        return "RAID5" if total_disks >= 3 else "NONE"


@pure
def parse_availability_delivery(status: str) -> tuple[int, str]:
    """Parse an OVH availability status into (delivery_hours, stock_level).

    e.g. '1H-low' -> (1, 'low'); '1H-high' -> (1, 'high'); '72H' -> (72, ''); unrecognized -> (0, '').
    """
    match = _AVAILABILITY_DELIVERY_RE.match(status)
    delivery_hours = int(match.group(1)) if match else 0
    stock_level = status.split("-", 1)[1] if "-" in status else ""
    return delivery_hours, stock_level


# Stock levels ranked best-first, so a tie on delivery time prefers the better-stocked region.
_STOCK_RANK_BY_LEVEL: Final[Mapping[str, int]] = {"high": 2, "low": 1, "": 0}


@pure
def _summarize_fastest_delivery(region_to_status: Mapping[str, str]) -> tuple[int, str]:
    """Across a config's per-region availability, return the (delivery_hours, stock_level) of the soonest.

    Ties on delivery time prefer the better-stocked region (high > low > unspecified).
    """
    fastest_hours = 0
    fastest_stock = ""
    for status in region_to_status.values():
        delivery_hours, stock_level = parse_availability_delivery(status)
        if delivery_hours <= 0:
            continue
        is_sooner = fastest_hours == 0 or delivery_hours < fastest_hours
        is_better_stocked_tie = delivery_hours == fastest_hours and _STOCK_RANK_BY_LEVEL.get(
            stock_level, 0
        ) > _STOCK_RANK_BY_LEVEL.get(fastest_stock, 0)
        if is_sooner or is_better_stocked_tie:
            fastest_hours = delivery_hours
            fastest_stock = stock_level
    return fastest_hours, fastest_stock


@pure
def _server_cpu_specs(products_by_name: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[int, int, str] | None:
    """Return (cpu_cores, cpu_threads, server_model) for a plan's product, or None if specs are absent."""
    product = products_by_name.get(str(plan.get("product")))
    if product is None:
        return None
    cpu = (((product.get("blobs") or {}).get("technical") or {}).get("server") or {}).get("cpu") or {}
    cores = cpu.get("cores")
    threads = cpu.get("threads")
    if not isinstance(cores, int) or not isinstance(threads, int) or threads <= 0:
        return None
    server_model = str(product.get("description") or plan.get("invoiceName") or plan.get("planCode"))
    return cores, threads, server_model


@pure
def _addon_family_codes(plan: Mapping[str, Any], family_name: str) -> tuple[str, ...]:
    """Return the add-on planCodes in a plan's named add-on family (empty if the family is absent)."""
    for family in plan.get("addonFamilies", []):
        if family.get("name") == family_name:
            return tuple(str(code) for code in family.get("addons", []))
    return ()


@pure
def _strip_plan_suffix(addon_code: str, plan_code: str) -> str:
    """Strip the trailing '-<planCode>' that catalog add-on codes carry, yielding the short availability code."""
    suffix = f"-{plan_code}"
    return addon_code[: -len(suffix)] if addon_code.endswith(suffix) else addon_code


@pure
def _build_availability_index(
    availabilities: Sequence[Mapping[str, Any]],
    allowed_regions: AbstractSet[str],
) -> dict[tuple[str, str], dict[str, dict[str, str]]]:
    """Index orderable combos as (planCode, memory_short) -> {storage_short -> {region -> availability status}}."""
    index: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
    for entry in availabilities:
        plan_code = entry.get("planCode")
        memory = entry.get("memory")
        storage = entry.get("storage")
        if not plan_code or not memory or not storage:
            continue
        region_to_status = {
            str(datacenter["datacenter"]): str(datacenter["availability"])
            for datacenter in entry.get("datacenters", [])
            if datacenter.get("datacenter") in allowed_regions
            and datacenter.get("availability") not in _UNORDERABLE_AVAILABILITY_STATUSES
        }
        if not region_to_status:
            continue
        storage_by_region = index.setdefault((str(plan_code), str(memory)), {})
        storage_by_region.setdefault(str(storage), {}).update(region_to_status)
    return index


@pure
def compute_slice_pricing_rows(
    catalog: Mapping[str, Any],
    availabilities: Sequence[Mapping[str, Any]],
    allowed_regions: AbstractSet[str],
    memory_per_slice_gb: int,
    cpu_overcommit_ratio: float,
) -> list[SlicePricingRow]:
    """Build the per-slice pricing rows (one per server x in-region RAM config), sorted cheapest-per-slice first.

    Each row prices the cheapest in-region storage as its base (used for the disk/slice and price/slice
    columns) and lists the other in-region storage configs as per-slice disk upgrades. Price per slice is the
    month-to-month cost plus the setup fee amortized over a year, divided by the server's slot count.
    Combos that cannot be priced month-to-month or cannot be sliced at the requested size are skipped.
    """
    products_by_name = {str(product["name"]): product for product in catalog.get("products", [])}
    availability_index = _build_availability_index(availabilities, allowed_regions)

    rows: list[SlicePricingRow] = []
    for plan in catalog.get("plans", []):
        plan_code = str(plan["planCode"])
        specs = _server_cpu_specs(products_by_name, plan)
        if specs is None:
            continue
        cpu_cores, cpu_threads, server_model = specs

        for memory_code in _addon_family_codes(plan, "memory"):
            memory_short = _strip_plan_suffix(memory_code, plan_code)
            available_storage_regions = availability_index.get((plan_code, memory_short))
            if not available_storage_regions:
                continue
            try:
                server_ram_gb = parse_memory_gb(memory_code)
            except OvhCatalogPricingError:
                continue
            slot_count = compute_slot_count(server_ram_gb, memory_per_slice_gb)
            if slot_count <= 0:
                continue

            # Price every in-region storage option for this (plan, memory); the cheapest is the row's base.
            priced_storages: list[tuple[Decimal, Decimal, str, int, str, dict[str, str]]] = []
            for storage_short, region_to_status in available_storage_regions.items():
                storage_code = f"{storage_short}-{plan_code}"
                try:
                    pricing = compute_order_pricing(catalog, plan_code, [memory_code, storage_code])
                    usable_gb = compute_storage_usable_gb(storage_short)
                except OvhCatalogPricingError:
                    continue
                raid_level = describe_storage_raid_level(storage_short)
                priced_storages.append(
                    (
                        pricing.recurring_monthly,
                        pricing.one_time_setup,
                        storage_short,
                        usable_gb,
                        raid_level,
                        region_to_status,
                    )
                )
            if not priced_storages:
                continue
            priced_storages.sort(key=lambda priced: (priced[0], priced[3]))
            base_monthly, base_setup, base_storage_label, base_usable_gb, _base_raid, base_region_status = (
                priced_storages[0]
            )

            try:
                disk_gb_per_slice = compute_slice_disk_budget_gib(base_usable_gb, slot_count)
                # Validate the budget can actually host a slice (boot disk + a positive data disk); skip if not.
                compute_slice_disk_gib(base_usable_gb, slot_count)
            except BareMetalConfigError:
                continue
            cpus_per_slice = compute_slice_vcpus(cpu_threads, slot_count, cpu_overcommit_ratio)
            delivery_hours, stock_level = _summarize_fastest_delivery(base_region_status)

            amortized_monthly = base_monthly + base_setup / _SETUP_AMORTIZATION_MONTHS
            price_per_slice = (amortized_monthly / Decimal(slot_count)).quantize(Decimal("0.01"))

            # The non-base storage options that add usable capacity, as per-slice disk upgrades.
            storage_options: list[SliceStorageOption] = []
            for monthly, _setup, storage_label, usable_gb, raid_level, _region_status in priced_storages:
                extra_usable_gb = usable_gb - base_usable_gb
                if extra_usable_gb <= 0:
                    continue
                storage_options.append(
                    SliceStorageOption(
                        storage_plan_code=f"{storage_label}-{plan_code}",
                        label=storage_label,
                        raid_level=raid_level,
                        usable_disk_gb=usable_gb,
                        extra_disk_gb_per_slice=extra_usable_gb // slot_count,
                        extra_monthly_usd=monthly - base_monthly,
                        dollars_per_extra_gb=((monthly - base_monthly) / Decimal(extra_usable_gb)).quantize(
                            Decimal("0.0001")
                        ),
                    )
                )
            storage_options.sort(key=lambda option: option.usable_disk_gb)

            available_regions = set().union(*(region_status for *_rest, region_status in priced_storages))
            rows.append(
                SlicePricingRow(
                    plan_code=plan_code,
                    server_model=server_model,
                    available_regions=tuple(sorted(available_regions)),
                    delivery_hours=delivery_hours,
                    stock_level=stock_level,
                    server_ram_gb=server_ram_gb,
                    cpu_cores=cpu_cores,
                    cpu_threads=cpu_threads,
                    memory_per_slice_gb=memory_per_slice_gb,
                    slot_count=slot_count,
                    cpus_per_slice=cpus_per_slice,
                    disk_gb_per_slice=disk_gb_per_slice,
                    base_storage_label=base_storage_label,
                    recurring_monthly_usd=base_monthly,
                    one_time_setup_usd=base_setup,
                    amortized_monthly_usd=amortized_monthly,
                    price_per_slice_usd=price_per_slice,
                    storage_options=tuple(storage_options),
                )
            )

    rows.sort(key=lambda row: row.price_per_slice_usd)
    return rows
