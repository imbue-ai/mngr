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


def test_load_schedule_provider_unsupported_type_raises(temp_mngr_ctx: MngrContext) -> None:
    """A provider that loads successfully but is neither local nor modal should raise.

    The 'lima' backend is registered and defers all installation/version checks
    to first use, so it instantiates cleanly here but is not a supported
    schedule provider. This exercises the third outcome of
    load_schedule_provider (load succeeds, type is unsupported), distinct from
    the load-failure path above.
    """
    with pytest.raises(click.ClickException, match="not supported for schedules"):
        load_schedule_provider("lima", temp_mngr_ctx)
