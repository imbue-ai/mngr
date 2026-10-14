import json
from pathlib import Path

import pytest

from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.agent_setup import register_agent_for_host
from imbue.mngr_latchkey.baseline_permissions import AGENT_BASELINE_PERMISSIONS
from imbue.mngr_latchkey.baseline_permissions import SCOPE_LATCHKEY_SELF
from imbue.mngr_latchkey.baseline_permissions import SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED
from imbue.mngr_latchkey.remote._machine_policy import project_policy_for_machine
from imbue.mngr_latchkey.remote._machine_policy import restore_policy_from_machine
from imbue.mngr_latchkey.remote.mock_outer_host_test import SLACK_GRANTED
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import dump_permissions_text
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.testing import rule_keys_of_permissions_json

_BASELINE_JSON = dump_permissions_text(AGENT_BASELINE_PERMISSIONS)


def _registered_host_policy(tmp_path: Path) -> str:
    """A host's policy as the plugin writes it once an agent is registered on the host."""
    host_id = HostId.generate()
    register_agent_for_host(tmp_path, host_id, AgentId.generate())
    return permissions_path_for_host(tmp_path, host_id).read_text()


def test_the_machines_copy_drops_the_gate_rule_and_keeps_its_schema() -> None:
    projected = project_policy_for_machine(_BASELINE_JSON)

    assert rule_keys_of_permissions_json(projected) == [
        key
        for key in rule_keys_of_permissions_json(_BASELINE_JSON)
        if key != SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED
    ]
    assert json.loads(projected)["schemas"] == json.loads(_BASELINE_JSON)["schemas"]


def test_a_policy_without_the_gate_travels_byte_for_byte() -> None:
    assert project_policy_for_machine(SLACK_GRANTED) == SLACK_GRANTED


def test_restoring_the_machines_copy_gives_back_what_was_pushed(tmp_path: Path) -> None:
    """The projection is exact, so adopting the machine's copy loses nothing here -- registered agents included."""
    policy = _registered_host_policy(tmp_path)

    restored = restore_policy_from_machine(project_policy_for_machine(policy))

    assert restored == policy


def test_restoring_puts_the_gate_back_ahead_of_the_gateway_self_rule() -> None:
    restored = restore_policy_from_machine(project_policy_for_machine(_BASELINE_JSON))

    keys = rule_keys_of_permissions_json(restored)
    assert keys.index(SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED) == keys.index(SCOPE_LATCHKEY_SELF) - 1


def test_restoring_appends_the_gate_to_a_policy_with_no_gateway_self_rule() -> None:
    config = LatchkeyPermissionsConfig(rules=({"slack-api": ["any"]},), schemas=AGENT_BASELINE_PERMISSIONS.schemas)

    restored = restore_policy_from_machine(dump_permissions_text(config))

    assert rule_keys_of_permissions_json(restored) == ["slack-api", SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED]


@pytest.mark.parametrize("policy", [_BASELINE_JSON, SLACK_GRANTED])
def test_restoring_leaves_a_policy_with_nothing_to_restore_alone(policy: str) -> None:
    """One already carrying the gate, and one that never defined it (not a host policy the plugin seeded)."""
    assert restore_policy_from_machine(policy) == policy


@pytest.mark.parametrize("text", ["not json", '{"rules": "not a list"}'])
def test_text_this_build_cannot_read_travels_unchanged_both_ways(text: str) -> None:
    assert project_policy_for_machine(text) == text
    assert restore_policy_from_machine(text) == text
