"""Shared fixtures for the ``minds.envs`` unit tests.

CLAUDE.md requires fixtures to live in a ``conftest.py`` rather than being
re-defined per ``_test.py`` file. These two were previously duplicated across
``docker_cleanup_test.py``, ``generation_test.py``, ``provisioning_test.py`` and
``local_store_test.py`` (and had drifted apart -- e.g. some entered the
ConcurrencyGroup, some did not). They are consolidated here.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup


@pytest.fixture
def _root_cg() -> Iterator[ConcurrencyGroup]:
    """An ACTIVE ConcurrencyGroup the tests can use as a subprocess parent.

    Entered (via ``with``) so suites that spawn real subprocesses -- the
    ``vault`` fake in ``generation_test`` and the ``docker`` CLI in
    ``docker_cleanup_test`` -- have an active parent, which
    ``run_process_to_completion`` requires. ``provisioning_test``'s fakes never
    spawn anything, but an active group is a harmless superset for them.
    """
    cg = ConcurrencyGroup(name="envs-test-root")
    with cg:
        yield cg


@pytest.fixture
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Layer the minds-specific tmp-home setup on top of the autouse isolation.

    The autouse ``setup_test_mngr_env`` fixture (registered in
    ``apps/minds/conftest.py``) already points ``HOME`` at this same ``tmp_path``
    and chdirs into it via ``isolate_home``. Here we only add the minds-specific
    bits:

    * clear ``MINDS_ROOT_NAME`` so the dev-env-name path computations derive the
      path purely from the ``DevEnvName``; and
    * seed an ``apps/`` marker so ``find_monorepo_root`` (called by ``deploy_env``
      to locate the recover-target file) resolves a root under the tmp tree
      instead of the real repo. Harmless for tests that don't deploy.
    """
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)
    (tmp_path / "apps").mkdir(exist_ok=True)
    return tmp_path
