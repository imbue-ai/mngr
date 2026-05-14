import pytest

from imbue.mngr.cli.agent_utils import select_agent_interactively_with_host
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError


def test_select_agent_interactively_raises_when_no_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """With a fresh context (no hosts or agents), raises UserInputError."""
    with pytest.raises(UserInputError, match="No agents found"):
        select_agent_interactively_with_host(temp_mngr_ctx)
