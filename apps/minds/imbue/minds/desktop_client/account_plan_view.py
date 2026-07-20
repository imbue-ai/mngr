"""Pure view-model construction for the accounts page's plan/usage section.

Turns the raw ``mngr imbue_cloud account show`` JSON into the display rows
the Accounts template renders: one row per quota with a human-readable label,
formatted usage, and formatted limit.
"""

from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure

_BYTES_PER_GB: Final[int] = 1024**3


@pure
def format_gb(byte_count: int) -> str:
    """Format a byte count as gigabytes with one decimal (e.g. '1.5 GB')."""
    return f"{byte_count / _BYTES_PER_GB:.1f} GB"


@pure
def _plan_display_name(plan_name: str) -> str:
    return plan_name.capitalize()


@pure
def _int_field(source: dict[str, Any], key: str) -> int:
    raw = source.get(key)
    return int(raw) if raw is not None else 0


@pure
def build_account_plan_view(info: dict[str, Any]) -> dict[str, Any]:
    """Build the template view model from the connector's account-info JSON.

    Returns ``{plan_name, plan_display_name, available_plans, usage_rows}``
    where each usage row is ``{label, used, limit, note}`` (all strings).
    """
    entitlements = info.get("entitlements") or {}
    usage = info.get("usage") or {}

    usage_rows: list[dict[str, str]] = [
        {
            "label": "Remote workspaces",
            "used": str(_int_field(usage, "remote_workspaces")),
            "limit": str(_int_field(entitlements, "max_remote_workspaces")),
            "note": "Stopped remote workspaces still count until destroyed.",
        },
        {
            "label": "Shared links (tunnels)",
            "used": str(_int_field(usage, "tunnels")),
            "limit": str(_int_field(entitlements, "max_tunnels")),
            "note": f"Up to {_int_field(entitlements, 'max_services_per_tunnel')} shared services per workspace.",
        },
        {
            "label": "Backup storage",
            "used": format_gb(_int_field(usage, "total_bucket_bytes")),
            "limit": format_gb(_int_field(entitlements, "max_total_bucket_bytes")),
            "note": "Total across all storage buckets; backups turn read-only while over the limit.",
        },
        {
            "label": "Storage buckets",
            "used": str(_int_field(usage, "buckets")),
            "limit": str(_int_field(entitlements, "max_buckets")),
            "note": "",
        },
        {
            "label": "AI spend (Imbue Cloud)",
            "used": f"${float(usage.get('llm_spend_usd_this_period') or 0):.2f}",
            "limit": f"${float(entitlements.get('monthly_llm_spend_usd') or 0):.2f} / month",
            "note": ("Applies only to Imbue-Cloud-provided AI; your own subscription or API key is never limited."),
        },
        {
            "label": "Synced workspaces",
            "used": str(_int_field(usage, "active_synced_workspaces")),
            "limit": str(_int_field(entitlements, "max_active_synced_workspaces")),
            "note": "",
        },
    ]
    plan_name = str(info.get("plan_name") or "")
    available = info.get("available_plans") or []
    return {
        "plan_name": plan_name,
        "plan_display_name": _plan_display_name(plan_name),
        "available_plans": [str(p) for p in available if isinstance(p, str)],
        "usage_rows": usage_rows,
    }
