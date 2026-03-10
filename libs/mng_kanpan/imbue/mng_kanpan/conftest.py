"""Test fixtures for mng-kanpan.

Uses shared plugin test fixtures from mng for common setup (plugin manager,
environment isolation, git repos, temp_mng_ctx, local_provider, etc.).
"""

from collections.abc import Generator
from typing import Any

import pytest

from imbue.mng.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())


def _fake_run_kanpan(
    called_with: list[dict[str, Any]],
) -> Any:
    """Return a callable that records run_kanpan invocations into *called_with*."""

    def _inner(
        mng_ctx: object,
        include_filters: tuple[str, ...] = (),
        exclude_filters: tuple[str, ...] = (),
    ) -> None:
        called_with.append(
            {"mng_ctx": mng_ctx, "include_filters": include_filters, "exclude_filters": exclude_filters}
        )

    return _inner


@pytest.fixture
def patched_run_kanpan(monkeypatch: pytest.MonkeyPatch) -> Generator[list[dict[str, Any]], None, None]:
    """Monkeypatch run_kanpan and yield the list of captured call dicts."""
    called_with: list[dict[str, Any]] = []
    monkeypatch.setattr("imbue.mng_kanpan.cli.run_kanpan", _fake_run_kanpan(called_with))
    yield called_with
