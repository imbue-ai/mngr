import pytest

from imbue.mngr.agents.update_policy import AgentUpdatePolicy
from imbue.mngr.agents.update_policy import resolve_update_policy


@pytest.mark.parametrize("configured", list(AgentUpdatePolicy))
@pytest.mark.parametrize("is_unattended", [True, False])
@pytest.mark.parametrize("is_ask_capable", [True, False])
def test_explicit_policy_always_wins(configured: AgentUpdatePolicy, is_unattended: bool, is_ask_capable: bool) -> None:
    assert resolve_update_policy(configured, is_unattended=is_unattended, is_ask_capable=is_ask_capable) == configured


def test_unattended_default_is_never() -> None:
    # Unattended takes precedence over ask-capability when unset.
    assert resolve_update_policy(None, is_unattended=True, is_ask_capable=True) == AgentUpdatePolicy.NEVER
    assert resolve_update_policy(None, is_unattended=True, is_ask_capable=False) == AgentUpdatePolicy.NEVER


def test_attended_default_is_ask_when_ask_capable() -> None:
    assert resolve_update_policy(None, is_unattended=False, is_ask_capable=True) == AgentUpdatePolicy.ASK


def test_attended_default_is_auto_without_ask_flow() -> None:
    assert resolve_update_policy(None, is_unattended=False, is_ask_capable=False) == AgentUpdatePolicy.AUTO
