"""Integration tests for file target resolution using real local provider."""

import pytest

from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import UserInputError
from imbue.mng_file.cli.target import resolve_file_target
from imbue.mng_file.data_types import PathRelativeTo


def test_resolve_file_target_raises_for_nonexistent_target(temp_mng_ctx: MngContext) -> None:
    with pytest.raises(UserInputError, match="No agent or host found"):
        resolve_file_target(
            target_identifier="nonexistent-target-abc123xyz",
            mng_ctx=temp_mng_ctx,
            relative_to=PathRelativeTo.WORK,
        )


def test_resolve_file_target_resolves_local_host(temp_mng_ctx: MngContext) -> None:
    result = resolve_file_target(
        target_identifier="localhost",
        mng_ctx=temp_mng_ctx,
        relative_to=PathRelativeTo.HOST,
    )
    assert result.is_online
    assert not result.is_agent
    assert result.base_path.is_dir()


def test_resolve_file_target_host_rejects_relative_to_state(temp_mng_ctx: MngContext) -> None:
    with pytest.raises(UserInputError, match="only valid for agent targets"):
        resolve_file_target(
            target_identifier="localhost",
            mng_ctx=temp_mng_ctx,
            relative_to=PathRelativeTo.STATE,
        )
