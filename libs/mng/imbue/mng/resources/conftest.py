"""Shared test fixtures for shell script tests in the resources package."""

from __future__ import annotations

import textwrap

import pytest


@pytest.fixture()
def stub_mng_log_sh() -> str:
    """A no-op mng_log.sh stub for testing shell scripts that source it."""
    return textwrap.dedent("""\
        #!/bin/bash
        log_info() { :; }
        log_debug() { :; }
        log_warn() { :; }
        log_error() { :; }
    """)
