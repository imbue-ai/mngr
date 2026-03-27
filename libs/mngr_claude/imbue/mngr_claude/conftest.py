"""Shared test fixtures for the mngr_claude plugin."""

import textwrap
from pathlib import Path
from typing import Generator

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.testing import make_mngr_ctx


@pytest.fixture()
def stub_mngr_log_sh() -> str:
    """A no-op mngr_log.sh stub for testing shell scripts that source it."""
    return textwrap.dedent("""\
        #!/bin/bash
        mngr_timestamp() { date -u +"%Y-%m-%dT%H:%M:%S.000000000Z"; }
        log_info() { :; }
        log_debug() { :; }
        log_warn() { :; }
        log_error() { :; }
    """)


@pytest.fixture
def interactive_mngr_ctx(
    temp_config: MngrConfig, temp_profile_dir: Path, plugin_manager: pluggy.PluginManager
) -> Generator[MngrContext, None, None]:
    """Create an interactive MngrContext with a temporary host directory.

    Use this fixture when testing code paths that require is_interactive=True.
    """
    cg = ConcurrencyGroup(name="test-interactive")
    with cg:
        yield make_mngr_ctx(temp_config, plugin_manager, temp_profile_dir, is_interactive=True, concurrency_group=cg)
