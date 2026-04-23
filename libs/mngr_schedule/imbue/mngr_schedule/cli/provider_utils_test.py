"""Unit tests for the shared provider utilities."""

import click
import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_schedule.cli.provider_utils import load_schedule_provider


def test_load_schedule_provider_local(temp_mngr_ctx: MngrContext) -> None:
    """Loading the local provider should return a LocalProviderInstance."""
    provider = load_schedule_provider("local", temp_mngr_ctx)
    assert isinstance(provider, LocalProviderInstance)


def test_load_schedule_provider_unknown_raises(temp_mngr_ctx: MngrContext) -> None:
    """Loading an unknown provider should raise ClickException."""
    with pytest.raises(click.ClickException, match="Failed to load provider"):
        load_schedule_provider("nonexistent-provider-xyz", temp_mngr_ctx)
