"""Unit tests for the kanpan plugin's muted field generators.

The offline generator (`_muted_offline_field`) reads the persisted
`plugin.kanpan.muted` bit straight off a `DiscoveredAgent`'s certified data.
The online generator (`_muted_online_field`) reads the same bit off a live
agent's `get_plugin_data(PLUGIN_NAME)`. Both are also exercised end-to-end by
the acceptance tests (a real local agent flows through `list_agents`).
"""

from types import SimpleNamespace
from typing import Any

import pytest

from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr_kanpan.data_source import PLUGIN_NAME
from imbue.mngr_kanpan.plugin import _muted_offline_field
from imbue.mngr_kanpan.plugin import _muted_online_field
from imbue.mngr_kanpan.testing import make_discovered_agent
from imbue.mngr_kanpan.testing import make_host_details


def test_muted_offline_field_true_when_muted() -> None:
    ref = make_discovered_agent({"plugin": {"kanpan": {"muted": True}}})
    assert _muted_offline_field(ref, make_host_details()) is True


def test_muted_offline_field_none_when_explicitly_unmuted() -> None:
    # The generator is sparse: it returns None (omitting the field) rather than
    # False, so the board reads it back as unmuted via its `.get(..., False)`.
    ref = make_discovered_agent({"plugin": {"kanpan": {"muted": False}}})
    assert _muted_offline_field(ref, make_host_details()) is None


def test_muted_offline_field_none_when_certified_data_empty() -> None:
    assert _muted_offline_field(make_discovered_agent({}), make_host_details()) is None


def test_muted_offline_field_none_when_no_kanpan_plugin() -> None:
    ref = make_discovered_agent({"plugin": {}})
    assert _muted_offline_field(ref, make_host_details()) is None


def test_muted_offline_field_none_when_no_muted_key() -> None:
    ref = make_discovered_agent({"plugin": {"kanpan": {}}})
    assert _muted_offline_field(ref, make_host_details()) is None


@pytest.mark.parametrize(
    ("muted_value", "expected"),
    [
        # `is_muted` coerces with `bool(...)`, so truthy non-bool values are
        # muted and falsy ones are not. This pins the coercion contract that a
        # naive `... is True` check would get wrong.
        (1, True),
        ("true", True),
        # Any non-empty string is truthy, even one that reads as "false".
        ("false", True),
        (0, None),
        ("", None),
    ],
)
def test_muted_offline_field_coerces_non_bool_muted_value(muted_value: Any, expected: bool | None) -> None:
    ref = make_discovered_agent({"plugin": {"kanpan": {"muted": muted_value}}})
    assert _muted_offline_field(ref, make_host_details()) is expected


def _online_agent(kanpan_plugin_data: dict[str, Any]) -> AgentInterface:
    """Fake agent exposing only the `get_plugin_data` seam the online generator uses."""
    return SimpleNamespace(get_plugin_data=lambda name: kanpan_plugin_data if name == PLUGIN_NAME else {})  # ty: ignore[invalid-return-type]


@pytest.mark.parametrize(
    ("kanpan_plugin_data", "expected"),
    [
        ({"muted": True}, True),
        ({"muted": False}, None),
        ({}, None),
        ({"muted": 1}, True),
        ({"muted": 0}, None),
        ({"muted": ""}, None),
    ],
)
def test_muted_online_field(kanpan_plugin_data: dict[str, Any], expected: bool | None) -> None:
    # The host argument is unused by the generator; None exercises that contract.
    assert _muted_online_field(_online_agent(kanpan_plugin_data), None) is expected  # ty: ignore[invalid-argument-type]
