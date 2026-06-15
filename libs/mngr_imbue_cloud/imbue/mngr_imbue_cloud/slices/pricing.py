from collections.abc import Mapping
from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.data_types import OrderPricing
from imbue.mngr_imbue_cloud.data_types import PriceLineItem
from imbue.mngr_imbue_cloud.errors import OvhCatalogPricingError

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
