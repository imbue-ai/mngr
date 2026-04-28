"""Project-level conftest for mngr.

When running tests from libs/mngr/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).
"""

from collections.abc import Generator
from typing import Any

import pytest
from loguru import logger

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.register_guards_docker import register_docker_cli_guard
from imbue.mngr.register_guards_docker import register_docker_sdk_guard
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.testing import _WARNINGS_ALLOWED_DEPTH
from imbue.resource_guards.resource_guards import register_resource_guard

suppress_warnings()

register_resource_guard("tmux")
register_resource_guard("modal")
register_resource_guard("rsync")
register_resource_guard("unison")
register_docker_cli_guard()
register_docker_sdk_guard()

register_marker("allow_warnings: opt out of the autouse 'no unexpected loguru warnings' check for this test")

register_conftest_hooks(globals())


# Per-test buffer of WARNING-or-higher loguru records the autouse fixture is
# watching. Reset by each fixture invocation. xdist workers are separate
# processes so this module-level state is per-worker.
_unexpected_warnings: list[str] = []


def _unexpected_warning_sink(message: Any) -> None:
    """Loguru sink that records WARNING+ records when not opted out."""
    if _WARNINGS_ALLOWED_DEPTH[0] > 0:
        return
    _unexpected_warnings.append(str(message).rstrip("\n"))


@pytest.fixture(autouse=True)
def fail_on_unexpected_loguru_warnings(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """Fail any test that emits a loguru WARNING-level (or higher) record.

    Opt-out mechanisms:
      * ``@pytest.mark.allow_warnings`` -- whole-test opt-out.
      * ``with allow_warnings(): ...`` -- fine-grained opt-out within a test.
      * Use of ``capture_loguru`` -- implicitly opts out for the duration of
        its context (since such tests are inspecting warnings on purpose).
    """
    marker = request.node.get_closest_marker("allow_warnings")
    if marker is not None:
        _WARNINGS_ALLOWED_DEPTH[0] += 1

    _unexpected_warnings.clear()
    sink_id = logger.add(_unexpected_warning_sink, level="WARNING", format="{message}")
    try:
        yield
    finally:
        # Some tests call setup_logging() which invokes logger.remove() (no arg)
        # and removes all handlers including ours. In that case our sink is
        # already gone, so swallow the resulting ValueError.
        try:
            logger.remove(sink_id)
        except ValueError:
            pass
        if marker is not None:
            _WARNINGS_ALLOWED_DEPTH[0] -= 1

        if _unexpected_warnings:
            captured = list(_unexpected_warnings)
            _unexpected_warnings.clear()
            joined = "\n".join(f"  - {msg}" for msg in captured)
            pytest.fail(
                f"Test emitted {len(captured)} unexpected loguru WARNING-or-higher "
                f"record(s):\n{joined}\n"
                "Wrap the emitting code in `with allow_warnings():` (from "
                "imbue.mngr.utils.testing) or mark the test with "
                "@pytest.mark.allow_warnings if the warnings are expected.",
                pytrace=False,
            )
