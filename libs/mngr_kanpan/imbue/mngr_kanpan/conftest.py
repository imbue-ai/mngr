"""Test fixtures for mngr-kanpan.

Uses shared plugin test fixtures from mngr for common setup (plugin manager,
environment isolation, git repos, temp_mngr_ctx, local_provider, etc.).
"""

import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

# The tmux mark is registered globally via the resource_guards entry
# point group; no per-project mark registration is needed.
register_plugin_test_fixtures(globals())


@pytest.fixture
def test_cg() -> Generator[ConcurrencyGroup, None, None]:
    """Provide a ConcurrencyGroup for tests that need one."""
    with ConcurrencyGroup(name="test") as cg:
        yield cg


@pytest.fixture
def fake_gh_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a stand-in `gh` first on PATH and return that script's own path.

    Tests that want to observe how `gh` was invoked rewrite the script through
    this path, rather than searching PATH for it.
    """
    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir()
    gh_path = bin_dir / "gh"
    gh_path.write_text("#!/bin/sh\nexit 1\n")
    gh_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return gh_path


@pytest.fixture
def gh_response_path(tmp_path: Path, fake_gh_script: Path) -> Path:
    """Point the stand-in `gh` at a response file and return that file.

    Write a GitHub API response body to the returned path and the data sources
    run for real against it -- real argv, real subprocess, real stdout parsing.
    """
    response_path = tmp_path / "gh_response.json"
    response_path.write_text("{}")
    fake_gh_script.write_text(f'#!/bin/sh\nexec cat "{response_path}"\n')
    return response_path


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
