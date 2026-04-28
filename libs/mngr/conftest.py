"""Project-level conftest for mngr.

When running tests from libs/mngr/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).
"""

import re
from collections.abc import Generator
from typing import Any

import pytest
from loguru import logger

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.register_guards_docker import register_docker_cli_guard
from imbue.mngr.register_guards_docker import register_docker_sdk_guard
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.testing import _WARNINGS_ALLOWED_STACK
from imbue.resource_guards.resource_guards import register_resource_guard

suppress_warnings()

register_resource_guard("tmux")
register_resource_guard("modal")
register_resource_guard("rsync")
register_resource_guard("unison")
register_docker_cli_guard()
register_docker_sdk_guard()

register_marker(
    "allow_warnings(match=None): opt out of the autouse 'no unexpected loguru warnings' check; "
    "if match is given, only warnings matching the regex are allowed"
)

register_conftest_hooks(globals())


# Per-test buffer of WARNING-or-higher loguru records the autouse fixture is
# watching. Reset by each fixture invocation. xdist workers are separate
# processes so this module-level state is per-worker.
_unexpected_warnings: list[str] = []


def _unexpected_warning_sink(message: Any) -> None:
    """Loguru sink that records WARNING+ records when not opted out.

    The top frame of _WARNINGS_ALLOWED_STACK governs: if it is None, any
    warning is allowed; if it is a regex, only warnings whose message
    matches are allowed.
    """
    text = str(message).rstrip("\n")
    if _WARNINGS_ALLOWED_STACK:
        pattern = _WARNINGS_ALLOWED_STACK[-1]
        # Loguru wraps the message with level/timestamp prefix in str(message);
        # use the underlying record["message"] for clean regex matching.
        record_message = message.record["message"] if hasattr(message, "record") else text
        if pattern is None or pattern.search(record_message):
            return
    _unexpected_warnings.append(text)


# External-resource markers whose tests legitimately produce a high noise of
# operational warnings (Docker daemon errors, paramiko reconnects, modal sandbox
# noise, tmux server lifecycle messages). Auto-opt-out preserves the warning
# check for the vast majority of unit/integration tests without forcing every
# integration test to carry an explicit allow_warnings marker.
_AUTO_ALLOW_WARNINGS_MARKERS: frozenset[str] = frozenset(
    {"docker", "docker_sdk", "tmux", "modal", "acceptance", "release"}
)


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
      * Tests carrying an external-resource marker (docker, docker_sdk, tmux,
        modal, acceptance, release) are implicitly opted out, since they
        interact with real external systems and produce a high noise of
        legitimate operational warnings.
    """
    marker = request.node.get_closest_marker("allow_warnings")
    pushed_frame = False
    if marker is not None:
        match_arg = marker.kwargs.get("match")
        pattern = re.compile(match_arg) if match_arg is not None else None
        _WARNINGS_ALLOWED_STACK.append(pattern)
        pushed_frame = True
    elif any(request.node.get_closest_marker(m) is not None for m in _AUTO_ALLOW_WARNINGS_MARKERS):
        _WARNINGS_ALLOWED_STACK.append(None)
        pushed_frame = True

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
        if pushed_frame:
            _WARNINGS_ALLOWED_STACK.pop()

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
