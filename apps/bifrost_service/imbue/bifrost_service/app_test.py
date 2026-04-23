"""Unit tests for pure helpers in ``bifrost_service.app``.

End-to-end handler behavior is covered in ``test_bifrost_service.py``.
"""

import pytest
from inline_snapshot import snapshot

from imbue.bifrost_service.app import BudgetInfo
from imbue.bifrost_service.app import InvalidKeyNameError
from imbue.bifrost_service.app import VirtualKeyOwnershipError
from imbue.bifrost_service.app import _build_bifrost_config
from imbue.bifrost_service.app import _extract_budget_info
from imbue.bifrost_service.app import _to_create_key_response
from imbue.bifrost_service.app import _to_virtual_key_info
from imbue.bifrost_service.app import extract_short_name
from imbue.bifrost_service.app import is_owned_by
from imbue.bifrost_service.app import make_key_name

# --- make_key_name / extract_short_name / is_owned_by ---


def test_make_key_name_combines_prefix_and_short_name() -> None:
    result = make_key_name("ab12cd34ab12cd34", "my-key")

    assert result == snapshot("ab12cd34ab12cd34--my-key")


def test_make_key_name_rejects_short_name_containing_separator() -> None:
    with pytest.raises(InvalidKeyNameError):
        make_key_name("ab12cd34ab12cd34", "bad--name")


def test_extract_short_name_strips_prefix() -> None:
    short = extract_short_name("ab12cd34ab12cd34--my-key", "ab12cd34ab12cd34")

    assert short == "my-key"


def test_extract_short_name_raises_when_prefix_mismatches() -> None:
    with pytest.raises(VirtualKeyOwnershipError):
        extract_short_name("someone--else", "ab12cd34ab12cd34")


def test_extract_short_name_refuses_prefix_substring_match() -> None:
    """Key owned by ``ab12cd34`` must not be claimable by ``ab12``.

    Without the separator-based check, a shorter prefix would falsely match a
    longer one's keys.
    """
    with pytest.raises(VirtualKeyOwnershipError):
        extract_short_name("ab12cd34ab12cd34--my-key", "ab12")


def test_is_owned_by_returns_true_for_matching_prefix() -> None:
    assert is_owned_by("ab12cd34ab12cd34--foo", "ab12cd34ab12cd34")


def test_is_owned_by_returns_false_for_wrong_prefix() -> None:
    assert not is_owned_by("someoneelse--foo", "ab12cd34ab12cd34")


def test_is_owned_by_returns_false_for_prefix_substring() -> None:
    """Rejects short-prefix substring matches (see extract_short_name test)."""
    assert not is_owned_by("ab12cd34ab12cd34--foo", "ab12")


# --- _extract_budget_info ---


def test_extract_budget_info_returns_none_when_no_budgets() -> None:
    assert _extract_budget_info({"id": "vk-1", "name": "n", "budgets": []}) is None


def test_extract_budget_info_returns_none_when_budgets_key_missing() -> None:
    assert _extract_budget_info({"id": "vk-1", "name": "n"}) is None


def test_extract_budget_info_parses_first_budget() -> None:
    raw = {
        "id": "vk-1",
        "name": "n",
        "budgets": [
            {
                "max_limit": 50.0,
                "reset_duration": "1d",
                "current_usage": 7.5,
                "last_reset": "2026-04-22T00:00:00Z",
            }
        ],
    }

    budget = _extract_budget_info(raw)

    assert budget == BudgetInfo(
        max_limit=50.0,
        reset_duration="1d",
        current_usage=7.5,
        last_reset="2026-04-22T00:00:00Z",
    )


# --- _to_virtual_key_info / _to_create_key_response ---


def test_to_virtual_key_info_populates_short_name_from_user_prefix() -> None:
    raw = {
        "id": "vk-1",
        "name": "ab12cd34ab12cd34--my-key",
        "is_active": True,
        "budgets": [],
    }

    info = _to_virtual_key_info(raw, "ab12cd34ab12cd34")

    assert info.key_id == "vk-1"
    assert info.short_name == "my-key"
    assert info.is_active is True
    assert info.budget is None


def test_to_create_key_response_includes_value_returned_by_bifrost() -> None:
    raw = {
        "id": "vk-1",
        "name": "ab12cd34ab12cd34--my-key",
        "value": "sk-bf-abcdef",
        "is_active": True,
        "budgets": [{"max_limit": 100.0, "reset_duration": "1d", "current_usage": 0.0}],
    }

    response = _to_create_key_response(raw, "ab12cd34ab12cd34")

    assert response.value == "sk-bf-abcdef"
    assert response.budget is not None
    assert response.budget.max_limit == 100.0


def test_to_create_key_response_raises_when_value_missing() -> None:
    """Bifrost always returns ``value`` on create. A missing value is a server bug."""
    raw = {"id": "vk-1", "name": "ab12cd34ab12cd34--my-key", "is_active": True, "budgets": []}

    with pytest.raises(Exception, match="missing 'value'"):
        _to_create_key_response(raw, "ab12cd34ab12cd34")


# --- _build_bifrost_config ---


def test_build_bifrost_config_uses_env_var_references_for_secrets() -> None:
    """All secrets must go through bifrost's ``env.VAR`` syntax so raw values
    never hit the serialized JSON that lives on disk in the container.
    """
    config = _build_bifrost_config()

    assert config["encryption_key"] == {"value": "env.BIFROST_ENCRYPTION_KEY"}
    assert config["auth_config"]["bearer_token"] == {"value": "env.BIFROST_ADMIN_TOKEN"}
    provider_key = config["providers"]["anthropic"]["keys"][0]
    assert provider_key["value"] == "env.ANTHROPIC_API_KEY"


def test_build_bifrost_config_points_both_stores_at_postgres() -> None:
    config = _build_bifrost_config()

    assert config["config_store"]["type"] == "postgres"
    assert config["logs_store"]["type"] == "postgres"
    assert config["config_store"]["enabled"] is True
    assert config["logs_store"]["enabled"] is True


def test_build_bifrost_config_uses_different_dbs_for_config_and_logs() -> None:
    """Logs are write-heavy and kept in a separate database to reduce load."""
    config = _build_bifrost_config()

    assert config["config_store"]["config"]["db_name"] == {"value": "env.NEON_CONFIG_DB"}
    assert config["logs_store"]["config"]["db_name"] == {"value": "env.NEON_LOGS_DB"}


def test_build_bifrost_config_requires_ssl() -> None:
    """Neon connections must always be TLS-encrypted."""
    config = _build_bifrost_config()

    assert config["config_store"]["config"]["ssl_mode"] == {"value": "require"}
    assert config["logs_store"]["config"]["ssl_mode"] == {"value": "require"}
