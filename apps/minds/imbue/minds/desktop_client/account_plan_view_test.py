from imbue.minds.desktop_client.account_plan_view import build_account_plan_view
from imbue.minds.desktop_client.account_plan_view import format_gb


def _account_info() -> dict[str, object]:
    return {
        "plan_name": "explorer",
        "available_plans": ["ally", "explorer"],
        "entitlements": {
            "max_remote_workspaces": 2,
            "max_tunnels": 50,
            "max_services_per_tunnel": 10,
            "max_buckets": 5,
            "max_total_bucket_bytes": 50 * 1024**3,
            "monthly_llm_spend_usd": 0.0,
            "max_active_synced_workspaces": 200,
        },
        "usage": {
            "remote_workspaces": 1,
            "tunnels": 3,
            "buckets": 2,
            "total_bucket_bytes": int(1.5 * 1024**3),
            "llm_spend_usd_this_period": 12.345,
            "llm_budget_resets_at": "2026-08-01T00:00:00Z",
            "active_synced_workspaces": 4,
        },
    }


def test_format_gb_renders_one_decimal() -> None:
    assert format_gb(50 * 1024**3) == "50.0 GB"
    assert format_gb(int(1.5 * 1024**3)) == "1.5 GB"
    assert format_gb(0) == "0.0 GB"


def test_build_account_plan_view_maps_every_quota_row() -> None:
    view = build_account_plan_view(_account_info())
    assert view["plan_name"] == "explorer"
    assert view["plan_display_name"] == "Explorer"
    assert view["available_plans"] == ["ally", "explorer"]
    rows_by_label = {row["label"]: row for row in view["usage_rows"]}
    assert rows_by_label["Remote workspaces"]["used"] == "1"
    assert rows_by_label["Remote workspaces"]["limit"] == "2"
    assert rows_by_label["Shared links (tunnels)"]["used"] == "3"
    assert "10" in rows_by_label["Shared links (tunnels)"]["note"]
    assert rows_by_label["Backup storage"]["used"] == "1.5 GB"
    assert rows_by_label["Backup storage"]["limit"] == "50.0 GB"
    assert rows_by_label["AI spend (Imbue Cloud)"]["used"] == "$12.35"
    assert rows_by_label["AI spend (Imbue Cloud)"]["limit"] == "$0.00 / month"
    assert rows_by_label["Synced workspaces"]["used"] == "4"


def test_build_account_plan_view_flags_over_storage_quota() -> None:
    under = build_account_plan_view(_account_info())
    assert under["is_over_storage_quota"] is False
    over_info = _account_info()
    over_info["usage"] = {
        "remote_workspaces": 1,
        "tunnels": 3,
        "buckets": 2,
        "total_bucket_bytes": 51 * 1024**3,
        "llm_spend_usd_this_period": 12.345,
        "llm_budget_resets_at": "2026-08-01T00:00:00Z",
        "active_synced_workspaces": 4,
    }
    over = build_account_plan_view(over_info)
    assert over["is_over_storage_quota"] is True


def test_build_account_plan_view_tolerates_missing_fields() -> None:
    view = build_account_plan_view({})
    assert view["plan_name"] == ""
    assert view["available_plans"] == []
    rows_by_label = {row["label"]: row for row in view["usage_rows"]}
    assert rows_by_label["Remote workspaces"]["used"] == "0"
    assert rows_by_label["Backup storage"]["limit"] == "0.0 GB"
