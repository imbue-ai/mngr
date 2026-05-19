"""Integration tests for file target resolution using real local provider."""

import pytest

from imbue.mngr.api.address_parsers import parse_agent_or_host_address
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr_file.cli.target import resolve_file_target
from imbue.mngr_file.data_types import PathRelativeTo


def test_resolve_file_target_raises_for_nonexistent_agent(temp_mngr_ctx: MngrContext) -> None:
    with pytest.raises(UserInputError, match="Could not find agent"):
        resolve_file_target(
            target=parse_agent_or_host_address("nonexistent-target-abc123xyz"),
            mngr_ctx=temp_mngr_ctx,
            relative_to=PathRelativeTo.WORK,
        )


def test_resolve_file_target_resolves_local_host(temp_mngr_ctx: MngrContext) -> None:
    result = resolve_file_target(
        target=parse_agent_or_host_address("@localhost"),
        mngr_ctx=temp_mngr_ctx,
        relative_to=PathRelativeTo.HOST,
    )
    assert result.is_online
    assert not result.is_agent
    assert result.base_path.is_dir()


def test_resolve_file_target_host_rejects_relative_to_state(temp_mngr_ctx: MngrContext) -> None:
    with pytest.raises(UserInputError, match="only valid for agent targets"):
        resolve_file_target(
            target=parse_agent_or_host_address("@localhost"),
            mngr_ctx=temp_mngr_ctx,
            relative_to=PathRelativeTo.STATE,
        )
