import pytest

from imbue.mngr.agents.update_policy import AgentUpdatePolicy
from imbue.mngr.agents.update_policy import is_self_update_disabled
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


def test_attended_default_is_never_without_ask_flow() -> None:
    # Without an interactive update flow, the default blocks self-update (rather than
    # leaving the CLI's auto-updater on) so a managed agent stays on its installed version.
    assert resolve_update_policy(None, is_unattended=False, is_ask_capable=False) == AgentUpdatePolicy.NEVER


def test_is_self_update_disabled() -> None:
    # Unset (no ask flow) disables by default, attended or not.
    assert is_self_update_disabled(None, is_unattended=False) is True
    assert is_self_update_disabled(None, is_unattended=True) is True
    assert is_self_update_disabled(AgentUpdatePolicy.NEVER, is_unattended=False) is True
    # Explicit AUTO (and ASK, which has no flow here) leaves the self-updater on.
    assert is_self_update_disabled(AgentUpdatePolicy.AUTO, is_unattended=True) is False
    assert is_self_update_disabled(AgentUpdatePolicy.ASK, is_unattended=False) is False
    # An ask-capable agent prompts (ASK) rather than blocking, so not "disabled".
    assert is_self_update_disabled(None, is_unattended=False, is_ask_capable=True) is False
