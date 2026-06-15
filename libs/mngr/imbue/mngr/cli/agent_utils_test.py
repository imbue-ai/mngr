import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.cli.agent_utils import find_agent_by_address_or_interactively
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError

# =============================================================================
# find_agent_by_address_or_interactively tests
# =============================================================================


def test_find_agent_by_address_or_interactively_raises_when_no_agents_in_interactive_mode(
    temp_mngr_ctx: MngrContext,
) -> None:
    """In interactive mode with no agents, raises UserInputError before showing the selector."""
    interactive_ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().is_interactive, True))
    with pytest.raises(UserInputError, match="No agents found"):
        find_agent_by_address_or_interactively(
            mngr_ctx=interactive_ctx,
            address=None,
            host_filter=None,
        )


def test_find_agent_by_address_or_interactively_raises_when_no_address_in_non_interactive_mode(
    temp_mngr_ctx: MngrContext,
) -> None:
    """In non-interactive mode without an address, raises UserInputError rather than showing a selector."""
    with pytest.raises(UserInputError, match="not running in interactive mode"):
        find_agent_by_address_or_interactively(
            mngr_ctx=temp_mngr_ctx,
            address=None,
            host_filter=None,
        )
