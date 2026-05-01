"""Test fixtures for mngr-kanpan.

Uses shared plugin test fixtures from mngr for common setup (plugin manager,
environment isolation, git repos, temp_mngr_ctx, local_provider, etc.).
"""

from collections.abc import Generator
from typing import Any

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

# The tmux mark is registered globally via the imbue_resource_guards entry
# point group; no per-project mark registration is needed.
register_plugin_test_fixtures(globals())


@pytest.fixture
def test_cg() -> Generator[ConcurrencyGroup, None, None]:
    """Provide a ConcurrencyGroup for tests that need one."""
    with ConcurrencyGroup(name="test") as cg:
        yield cg


def _fake_run_kanpan(
    called_with: list[dict[str, Any]],
) -> Any:
    """Return a callable that records run_kanpan invocations into *called_with*."""

    def _inner(
        mngr_ctx: object,
        include_filters: tuple[str, ...] = (),
        exclude_filters: tuple[str, ...] = (),
    ) -> None:
        called_with.append(
            {"mngr_ctx": mngr_ctx, "include_filters": include_filters, "exclude_filters": exclude_filters}
        )

    return _inner


@pytest.fixture
def patched_run_kanpan(monkeypatch: pytest.MonkeyPatch) -> Generator[list[dict[str, Any]], None, None]:
    """Monkeypatch run_kanpan and yield the list of captured call dicts."""
    called_with: list[dict[str, Any]] = []
    monkeypatch.setattr("imbue.mngr_kanpan.cli.run_kanpan", _fake_run_kanpan(called_with))
    yield called_with
