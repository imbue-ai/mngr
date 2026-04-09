"""Shared test fixtures for the mngr_claude plugin."""

import textwrap

import pytest

from imbue.mngr.plugin_catalog import get_independent_entry_point_names


@pytest.fixture
def enabled_plugins() -> frozenset[str]:
    """Enable BASIC-tier plugins plus all claude-package entry points.

    The mngr_claude package provides claude (BASIC) plus code_guardian,
    fixme_fairy, and headless_claude (EXTRA). Tests in this package need
    all of them loaded.
    """
    return get_independent_entry_point_names() | {"code_guardian", "fixme_fairy", "headless_claude"}


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
